"""Daily quota + frozen submission outbox in one atomic Mongo document.

Regular accounts use bounded daily buckets. Administrators get one bounded
document per image, with no daily limit. The unique multikey index prevents
the same Telegram image entering another bucket or another day.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone as utc_timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.write_concern import WriteConcern

from imager_config.loader import config
from imager_contracts import JobRequest, payload_fingerprint
from src.infrastructure.data.errors import AdmissionConflict, AdmissionUncertain, DailyQuotaExceeded
from src.models.admission import AdmissionClaim, AdmissionReceipt


class AdmissionRepository:
    def __init__(self, collection: AsyncCollection, *, timezone, daily_limit, lease_seconds, retry_seconds):
        self.collection = collection.with_options(write_concern=WriteConcern(w=1, j=True))
        self.timezone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
        self.daily_limit = daily_limit
        self.lease_seconds = lease_seconds
        self.retry_seconds = retry_seconds
        if daily_limit <= 0 or lease_seconds <= 0 or retry_seconds <= 0:
            raise ValueError('Admission limits and lease/retry durations must be positive')

    async def initialize_indexes(self):
        await self.collection.create_index(
            'admissions.job_id', unique=True,
            partialFilterExpression={'admissions.job_id': {'$exists': True}},
        )
        await self.collection.create_index([('admissions.state', 1), ('admissions.next_attempt_at', 1)])
        await self.collection.create_index([
            ('actor_id', 1), ('date', -1), ('last_admitted_at', -1), ('created_at', -1),
        ])
        await self.collection.create_index([('actor_id', 1), ('date', 1), ('quota_exempt', 1)])
        await self.collection.create_index([
            ('admissions.state', 1), ('admissions.admin_notice.state', 1),
            ('admissions.admin_notice.next_attempt_at', 1),
        ])
        # Daily buckets (and their frozen request copies) are not kept forever.
        await self.collection.create_index(
            'created_at', name='admission_bucket_ttl',
            expireAfterSeconds=config.bot.admission_retention_days * 24 * 3600,
        )

    @staticmethod
    def _now(now):
        moment = now or datetime.now(utc_timezone.utc)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError('Admission time must include a timezone')
        return moment

    def _day(self, actor_id, now):
        local = now.astimezone(self.timezone)
        day = local.date().isoformat()
        reset = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), self.timezone)
        return f'{actor_id}:{day}', day, reset

    @staticmethod
    def _receipt(bucket, entry, *, duplicate):
        return AdmissionReceipt(
            job_id=entry['job_id'], state=entry['state'], date=bucket['date'],
            payload_hash=entry['payload_hash'], duplicate=duplicate, actor_id=bucket['actor_id'],
        )

    async def _find_entry(self, job_id):
        bucket = await self.collection.find_one({'admissions.job_id': job_id})
        if bucket is None:
            return None
        entry = next(entry for entry in bucket['admissions'] if entry['job_id'] == job_id)
        return bucket, entry

    async def find(self, job_id):
        found = await self._find_entry(job_id)
        return self._receipt(*found, duplicate=True) if found else None

    async def latest(self, actor_id):
        bucket = await self.collection.find_one(
            {'actor_id': actor_id, 'admissions.0': {'$exists': True}},
            {'actor_id': 1, 'date': 1, 'admissions': {'$slice': -1}},
            sort=[('date', -1), ('last_admitted_at', -1), ('created_at', -1)],
        )
        if not bucket or bucket.get('actor_id') != actor_id or not bucket.get('admissions'):
            return None
        return self._receipt(bucket, bucket['admissions'][-1], duplicate=True)

    async def latest_request(self, actor_id):
        """Recover reusable settings from the actor's durable accepted command."""
        bucket = await self.collection.find_one(
            {'actor_id': actor_id, 'admissions.0': {'$exists': True}},
            {'actor_id': 1, 'admissions': {'$slice': -1}},
            sort=[('date', -1), ('last_admitted_at', -1), ('created_at', -1)],
        )
        if not bucket or bucket.get('actor_id') != actor_id or not bucket.get('admissions'):
            return None
        document = bucket['admissions'][-1]['document']
        self._validate_document(document, actor_id)
        return deepcopy(document['request'])

    async def remaining(self, actor_id, now=None):
        bucket_id, day, _ = self._day(actor_id, self._now(now))
        document = await self.collection.find_one({'_id': bucket_id}, {'used': 1})
        exempt_used = await self._exempt_count(actor_id, day)
        return max(0, self.daily_limit - exempt_used - (document['used'] if document else 0))

    async def _exempt_count(self, actor_id, day):
        # Kept separately so revoking an administrator does not reset the
        # ordinary account's usage or lose accepted-but-not-published images.
        return await self.collection.count_documents({
            'actor_id': actor_id, 'date': day, 'quota_exempt': True, 'refunded': {'$ne': True},
        })

    def day_window(self, now=None):
        local = self._now(now).astimezone(self.timezone)
        start = datetime.combine(local.date(), datetime.min.time(), self.timezone)
        end = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), self.timezone)
        return start.astimezone(utc_timezone.utc), end.astimezone(utc_timezone.utc)

    async def day_exists(self, actor_id, now=None):
        bucket_id, _, _ = self._day(actor_id, self._now(now))
        return await self.collection.find_one({'_id': bucket_id}, {'_id': 1}) is not None

    async def ensure_day(self, actor_id, *, initial_used=0, now=None):
        if isinstance(initial_used, bool) or not isinstance(initial_used, int) or initial_used < 0:
            raise ValueError('Initial quota count must be a nonnegative integer')
        moment = self._now(now)
        bucket_id, day, _ = self._day(actor_id, moment)
        try:
            await self.collection.update_one({'_id': bucket_id}, {'$setOnInsert': {
                '_id': bucket_id, 'actor_id': actor_id, 'date': day,
                'used': min(initial_used, self.daily_limit), 'admissions': [], 'created_at': moment,
            }}, upsert=True)
        except DuplicateKeyError:
            # Another caller already initialized the day with its authoritative seed.
            if not await self.day_exists(actor_id, now=moment):
                raise

    @staticmethod
    def _validate_document(document, actor_id):
        request = JobRequest.from_dict(document['request'])
        if request.job_id != document['_id'] or payload_fingerprint(request) != document['payload_hash']:
            raise AdmissionConflict('Параметры изображения изменились. Выберите их заново.')
        if request.client_id != 'telegram' or request.client_context.get('user_id') != actor_id:
            raise AdmissionConflict('Изображение недоступно для этого аккаунта.')
        if document.get('status') != 'queued' or document.get('dispatch', {}).get('state') != 'pending':
            raise AdmissionConflict('Это изображение уже принято. Проверьте его статус.')
        if not document.get('execution_policy'):
            raise AdmissionConflict('Не удалось подготовить обработку изображения. Попробуйте ещё раз.')

    @staticmethod
    def _matching_receipt(found, document):
        bucket, entry = found
        if entry['payload_hash'] != document['payload_hash']:
            raise AdmissionConflict('Это сообщение уже принято с другими параметрами.')
        return AdmissionRepository._receipt(bucket, entry, duplicate=True)

    async def accept_submission(self, document, *, actor_id, now=None, quota_exempt=False):
        if type(quota_exempt) is not bool:
            raise ValueError('Quota exemption must be a resolved boolean policy')
        self._validate_document(document, actor_id)
        found = await self._find_entry(document['_id'])
        if found:
            return self._matching_receipt(found, document)
        moment = self._now(now)
        bucket_id, day, reset = self._day(actor_id, moment)
        entry = {
            'job_id': document['_id'], 'payload_hash': document['payload_hash'],
            'document': deepcopy(document), 'state': 'pending', 'attempts': 0,
            'token': None, 'lease_until': moment, 'next_attempt_at': moment,
            'accepted_at': moment, 'last_error': None,
            # Created with acceptance, so uncertain acknowledgements and replay
            # cannot lose or duplicate the notification. Legacy entries lack it.
            'admin_notice': {'state': 'pending', 'recipients': None, 'next_attempt_at': moment},
        }
        try:
            await self.ensure_day(actor_id, now=moment)
            if quota_exempt:
                # Never grow an administrator's daily array indefinitely.
                # The request's immutable identity is also this bucket's key.
                bucket_id = f'{actor_id}:admin:{document["_id"]}'
                bucket = {
                    '_id': bucket_id, 'actor_id': actor_id, 'date': day,
                    'quota_exempt': True, 'used': 1, 'admissions': [entry],
                    'created_at': moment, 'last_admitted_at': moment,
                }
                previous = await self.collection.find_one_and_update(
                    {'_id': bucket_id}, {'$setOnInsert': bucket},
                    upsert=True, return_document=ReturnDocument.BEFORE,
                )
                if previous is not None:
                    return self._matching_receipt((previous, previous['admissions'][0]), document)
            else:
                ordinary_limit = max(0, self.daily_limit - await self._exempt_count(actor_id, day))
                bucket = await self.collection.find_one_and_update(
                    {'_id': bucket_id, 'used': {'$lt': ordinary_limit},
                     'admissions.job_id': {'$ne': document['_id']}},
                    {'$inc': {'used': 1}, '$push': {'admissions': entry},
                     '$set': {'last_admitted_at': moment}},
                    return_document=ReturnDocument.AFTER,
                )
            if bucket:
                return self._receipt(bucket, entry, duplicate=False)
        except DuplicateKeyError:
            # The unique job index also spans yesterday's and tomorrow's buckets.
            pass
        except PyMongoError as error:
            try:
                found = await self._find_entry(document['_id'])
                if found:
                    return self._matching_receipt(found, document)
            except PyMongoError:
                pass
            raise AdmissionUncertain(document['_id']) from error
        found = await self._find_entry(document['_id'])
        if found:
            return self._matching_receipt(found, document)
        raise DailyQuotaExceeded(self.daily_limit, reset)

    async def refund(self, job_id, now=None):
        """Return the quota unit of a generation that failed in the core.

        Idempotent: the entry is flagged in the same atomic update, so retries
        and concurrent consumers can never return the unit twice. Exempt
        (administrator) admissions own a one-entry bucket that stops counting.
        """
        moment = self._now(now)
        ordinary = await self.collection.update_one(
            {'quota_exempt': {'$ne': True}, 'used': {'$gt': 0},
             'admissions': {'$elemMatch': {'job_id': job_id, 'refunded': {'$ne': True}}}},
            {'$inc': {'used': -1},
             '$set': {'admissions.$.refunded': True, 'admissions.$.refunded_at': moment}},
        )
        if ordinary.matched_count:
            return True
        exempt = await self.collection.update_one(
            {'quota_exempt': True, 'refunded': {'$ne': True}, 'admissions.job_id': job_id},
            {'$set': {'refunded': True, 'refunded_at': moment}},
        )
        return bool(exempt.matched_count)

    async def pending_count(self):
        cursor = await self.collection.aggregate([
            {'$match': {'admissions.state': {'$in': ['pending', 'claimed']}}},
            {'$project': {'pending': {'$size': {'$filter': {
                'input': '$admissions', 'as': 'entry',
                'cond': {'$in': ['$$entry.state', ['pending', 'claimed']]},
            }}}}},
            {'$group': {'_id': None, 'count': {'$sum': '$pending'}}},
        ])
        result = await cursor.to_list(length=1)
        return result[0]['count'] if result else 0

    async def pending_submissions(self, limit):
        cursor = await self.collection.aggregate([
            {'$match': {'admissions.state': {'$in': ['pending', 'claimed']}}},
            {'$unwind': '$admissions'},
            {'$match': {'admissions.state': {'$in': ['pending', 'claimed']}}},
            {'$replaceRoot': {'newRoot': '$admissions.document'}},
            {'$sort': {'created_at': 1, '_id': 1}}, {'$limit': limit},
            {'$project': {'_id': 1, 'created_at': 1, 'status': 1}},
        ])
        return await cursor.to_list(length=limit)

    async def claim(self, now=None):
        moment = self._now(now)
        eligible = {'$or': [
            {'state': 'pending', 'next_attempt_at': {'$lte': moment}},
            {'state': 'claimed', 'lease_until': {'$lte': moment}},
        ]}
        # A bucket's creation time is the account's first admission that day,
        # not the age of its next image. Select entries globally, then recheck
        # their eligibility in the atomic claim below.
        cursor = await self.collection.aggregate([
            {'$match': {'admissions': {'$elemMatch': eligible}}},
            {'$unwind': '$admissions'},
            {'$match': {'$or': [
                {'admissions.state': 'pending', 'admissions.next_attempt_at': {'$lte': moment}},
                {'admissions.state': 'claimed', 'admissions.lease_until': {'$lte': moment}},
            ]}},
            {'$sort': {'admissions.accepted_at': 1, 'admissions.job_id': 1}},
            {'$limit': 1},
            {'$project': {'_id': 1, 'job_id': '$admissions.job_id'}},
        ])
        candidates = await cursor.to_list(length=1)
        if not candidates:
            return None
        candidate = candidates[0]
        token = uuid4().hex
        bucket = await self.collection.find_one_and_update(
            {'_id': candidate['_id'],
             'admissions': {'$elemMatch': dict(eligible, job_id=candidate['job_id'])}},
            {'$set': {'admissions.$.state': 'claimed', 'admissions.$.token': token,
                      'admissions.$.lease_until': moment + timedelta(seconds=self.lease_seconds)},
             '$inc': {'admissions.$.attempts': 1}},
            return_document=ReturnDocument.AFTER,
        )
        if bucket is None:
            return None
        entry = next(entry for entry in bucket['admissions'] if entry.get('token') == token)
        return AdmissionClaim(entry['job_id'], token, deepcopy(entry['document']))

    @staticmethod
    def _claim_filter(job_id, token):
        return {'admissions': {'$elemMatch': {
            'job_id': job_id, 'token': token, 'state': 'claimed',
        }}}

    async def renew(self, job_id, token, now=None):
        result = await self.collection.update_one(
            self._claim_filter(job_id, token), {'$set': {
                'admissions.$.lease_until': self._now(now) + timedelta(seconds=self.lease_seconds),
            }},
        )
        return bool(result.matched_count)

    async def acknowledge(self, job_id, token, now=None):
        result = await self.collection.update_one(self._claim_filter(job_id, token), {'$set': {
            'admissions.$.state': 'sent', 'admissions.$.sent_at': self._now(now),
            'admissions.$.token': None, 'admissions.$.last_error': None,
        }})
        return bool(result.matched_count)

    async def retry(self, job_id, token, error, *, error_max_length, now=None):
        moment = self._now(now)
        result = await self.collection.update_one(self._claim_filter(job_id, token), {'$set': {
            'admissions.$.state': 'pending', 'admissions.$.token': None,
            'admissions.$.next_attempt_at': moment + timedelta(seconds=self.retry_seconds),
            'admissions.$.last_error': str(error)[:error_max_length],
        }})
        return bool(result.matched_count)

    async def next_admin_notice(self, now=None):
        """Only new, core-published admissions produce administrator notices."""
        moment = self._now(now)
        eligible = {
            'state': 'sent', 'admin_notice.state': 'pending',
            'admin_notice.next_attempt_at': {'$lte': moment},
        }
        cursor = await self.collection.aggregate([
            {'$match': {'admissions': {'$elemMatch': eligible}}},
            {'$unwind': '$admissions'},
            {'$match': {f'admissions.{key}': value for key, value in eligible.items()}},
            {'$sort': {'admissions.accepted_at': 1, 'admissions.job_id': 1}},
            {'$limit': 1},
            {'$project': {
                '_id': 0, 'actor_id': 1, 'job_id': '$admissions.job_id',
                'document': '$admissions.document', 'accepted_at': '$admissions.accepted_at',
                'admin_notice': '$admissions.admin_notice',
            }},
        ])
        rows = await cursor.to_list(length=1)
        return rows[0] if rows else None

    @staticmethod
    def _admin_recipient_key(admin_id):
        if type(admin_id) is not int or admin_id <= 0:
            raise ValueError('Administrator identity must be a positive integer')
        return str(admin_id)

    @staticmethod
    def _notice_filter(job_id, **conditions):
        return {'admissions': {'$elemMatch': {'job_id': job_id, **conditions}}}

    async def initialize_admin_notice(self, job_id, recipient_ids, now=None):
        """Freeze the fanout once; an empty recipient set is also initialized."""
        moment = self._now(now)
        keys = sorted({self._admin_recipient_key(admin_id) for admin_id in recipient_ids})
        result = await self.collection.update_one(
            self._notice_filter(job_id, state='sent', **{
                'admin_notice.state': 'pending', 'admin_notice.recipients': None,
            }),
            {'$set': {
                'admissions.$.admin_notice.recipients': {
                    key: {'state': 'pending', 'next_attempt_at': moment} for key in keys
                },
                'admissions.$.admin_notice.next_attempt_at': moment,
                'admissions.$.admin_notice.updated_at': moment,
            }},
        )
        return bool(result.matched_count)

    async def claim_admin_notice_recipient(self, job_id, admin_id, now=None):
        """Reserve one non-repeatable attempt before Telegram I/O.

        A process may die after Telegram accepted its request. Uncertain is a
        terminal state unless Telegram explicitly rejected the send (429).
        """
        moment = self._now(now)
        path = f'admin_notice.recipients.{self._admin_recipient_key(admin_id)}'
        result = await self.collection.update_one(
            self._notice_filter(job_id, state='sent', **{
                'admin_notice.state': 'pending', f'{path}.state': 'pending',
                f'{path}.next_attempt_at': {'$lte': moment},
            }),
            {'$set': {
                f'admissions.$.{path}.state': 'uncertain',
                f'admissions.$.{path}.attempted_at': moment,
                'admissions.$.admin_notice.updated_at': moment,
            }},
        )
        return bool(result.matched_count)

    async def update_admin_notice_recipient(
        self, job_id, admin_id, *, state, message_id=None, error=None, retry_at=None, now=None,
    ):
        """Record a known result; pending is reserved for a definite FloodWait."""
        moment = self._now(now)
        path = f'admin_notice.recipients.{self._admin_recipient_key(admin_id)}'
        if state not in {'sent', 'failed', 'uncertain', 'pending'}:
            raise ValueError('Unsupported administrator notification state')
        if message_id is not None and (type(message_id) is not int or message_id <= 0):
            raise ValueError('Telegram message identity must be a positive integer')
        if state == 'pending':
            if retry_at is None:
                raise ValueError('A definite Telegram rejection requires a retry time')
            retry_at = self._now(retry_at)
        elif retry_at is not None:
            raise ValueError('Only a definite Telegram rejection can be retried')
        fields = {
            f'admissions.$.{path}.state': state,
            f'admissions.$.{path}.updated_at': moment,
            'admissions.$.admin_notice.updated_at': moment,
        }
        if message_id is not None:
            fields[f'admissions.$.{path}.message_id'] = message_id
        if error is not None:
            fields[f'admissions.$.{path}.error'] = str(error)[:config.bot.error_max_length]
        update = {'$set': fields}
        if state == 'pending':
            fields[f'admissions.$.{path}.next_attempt_at'] = retry_at
            # A different consumer may have finalized the uncertain attempt.
            # Reopen only this definite rejection. Keep an earlier due sibling.
            fields['admissions.$.admin_notice.state'] = 'pending'
            update['$min'] = {'admissions.$.admin_notice.next_attempt_at': retry_at}
        result = await self.collection.update_one(
            self._notice_filter(job_id, **{f'{path}.state': 'uncertain'}), update,
        )
        return bool(result.matched_count)

    async def skip_admin_notice_recipient(self, job_id, admin_id, now=None):
        moment = self._now(now)
        path = f'admin_notice.recipients.{self._admin_recipient_key(admin_id)}'
        result = await self.collection.update_one(
            self._notice_filter(job_id, **{
                'admin_notice.state': 'pending', f'{path}.state': 'pending',
            }),
            {'$set': {
                f'admissions.$.{path}.state': 'skipped',
                f'admissions.$.{path}.updated_at': moment,
                'admissions.$.admin_notice.updated_at': moment,
            }},
        )
        return bool(result.matched_count)

    async def complete_admin_notice(self, job_id, now=None):
        """Finalize terminal fanout or defer to its earliest pending recipient.

        Matching the observed whole recipient map prevents a concurrent send or
        definite-rejection retry from being overwritten by this finalization.
        """
        moment = self._now(now)
        found = await self._find_entry(job_id)
        if found is None:
            return False
        notice = found[1].get('admin_notice')
        if not notice or notice.get('state') != 'pending' or notice.get('recipients') is None:
            return False
        recipients = notice['recipients']
        pending = [entry['next_attempt_at'] for entry in recipients.values() if entry['state'] == 'pending']
        fields = {'admissions.$.admin_notice.updated_at': moment}
        if pending:
            fields['admissions.$.admin_notice.next_attempt_at'] = min(pending)
        else:
            fields.update({
                'admissions.$.admin_notice.state': 'done',
                'admissions.$.admin_notice.completed_at': moment,
            })
        result = await self.collection.update_one(
            self._notice_filter(job_id, **{
                'admin_notice.state': 'pending', 'admin_notice.recipients': recipients,
            }), {'$set': fields},
        )
        return bool(result.matched_count) and not pending
