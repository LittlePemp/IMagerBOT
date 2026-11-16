"""Remove the legacy keyboard TTL without deleting conversation state."""
from pymongo.errors import OperationFailure


LEGACY_VIEW_EXPIRY = 'data._current_view.expires_on'
LEGACY_VIEW_EXPIRY_FIELDS = ('data._current_view.expires_at', LEGACY_VIEW_EXPIRY)


async def preserve_conversations(collection):
    """Migrate the old view expiration in place before starting bot polling.

    Aiogram keeps state and data in one Mongo document. A TTL on the keyboard's
    timestamp therefore removes the entire form, including the draft. Remove
    only that known TTL, then remove its old metadata while retaining the form.
    """
    removed = []
    for name, definition in (await collection.index_information()).items():
        keys = definition.get('key', [])
        keys = list(keys.items()) if isinstance(keys, dict) else list(keys)
        if keys != [(LEGACY_VIEW_EXPIRY, 1)] or definition.get('expireAfterSeconds') != 0:
            continue
        try:
            await collection.drop_index(name)
        except OperationFailure as error:
            if error.code != 27:  # Another startup already removed this index.
                raise
        removed.append(name)
    result = await collection.update_many(
        {'$or': [{field: {'$exists': True}} for field in LEGACY_VIEW_EXPIRY_FIELDS]},
        {'$unset': {field: '' for field in LEGACY_VIEW_EXPIRY_FIELDS}},
    )
    return {'removed_indexes': removed, 'cleaned_documents': result.modified_count}
