"""Daily quota + frozen submission outbox in one atomic Mongo document.

Every account, including administrators, has the same configured daily limit.
The unique multikey index prevents the same Telegram job entering another day.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone as utc_timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.write_concern import WriteConcern

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
        await self.collection.create_index([('actor_id', 1), ('date', -1)])

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
            sort=[('date', -1)],
        )
        if not bucket or bucket.get('actor_id') != actor_id or not bucket.get('admissions'):
            return None
        return self._receipt(bucket, bucket['admissions'][-1], duplicate=True)

    async def remaining(self, actor_id, now=None):
        bucket_id, _, _ = self._day(actor_id, self._now(now))
        document = await self.collection.find_one({'_id': bucket_id}, {'used': 1})
        return max(0, self.daily_limit - (document['used'] if document else 0))

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

    async def accept_submission(self, document, *, actor_id, now=None):
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
        }
        try:
            await self.ensure_day(actor_id, now=moment)
            bucket = await self.collection.find_one_and_update(
                {'_id': bucket_id, 'used': {'$lt': self.daily_limit},
                 'admissions.job_id': {'$ne': document['_id']}},
                {'$inc': {'used': 1}, '$push': {'admissions': entry}},
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
