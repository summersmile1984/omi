"""Owned object inventory shared by self-host purge and completion proof."""

import hashlib
import os

from firestore_pg.erasure import validate_uid
from .storage_minio import get_minio_client


def owned_prefixes(uid):
    uid = validate_uid(uid)
    configured = (
        ('BUCKET_SPEECH_PROFILES', (f'{uid}/',)),
        ('BUCKET_MEMORIES_RECORDINGS', (f'{uid}/',)),
        ('BUCKET_PRIVATE_CLOUD_SYNC', tuple(f'{kind}/{uid}/' for kind in ('chunks', 'audio', 'merged', 'playback'))),
        ('BUCKET_TEMPORAL_SYNC_LOCAL', (f'syncing/{uid}/',)),
        ('BUCKET_CHAT_FILES', (f'{uid}/',)),
        ('BUCKET_SCREEN_FRAMES', (f'{uid}/',)),
    )
    seen = set()
    for setting, prefixes in configured:
        name = os.environ.get(setting, '')
        if not name:
            continue
        for prefix in prefixes:
            if (name, prefix) not in seen:
                seen.add((name, prefix))
                yield name, prefix


def count_owner(uid):
    client = get_minio_client()
    return sum(1 for name, prefix in owned_prefixes(uid) for _ in client.bucket(name).list_blobs(prefix=prefix))


def purge_owner(uid):
    client = get_minio_client()
    deleted = 0
    for name, prefix in owned_prefixes(uid):
        bucket = client.bucket(name)
        for blob in bucket.list_blobs(prefix=prefix):
            blob.delete()
            deleted += 1
        if next(bucket.list_blobs(prefix=prefix), None) is not None:
            raise RuntimeError('MinIO owner purge left residual objects')
    return deleted


def signed_url(blob, minutes):
    from database.redis_db import cache_signed_url, get_cached_signed_url
    from datetime import timedelta

    if type(minutes) is not int or not 1 <= minutes <= 10080:
        raise ValueError('private object link duration must be 1..10080 minutes')
    # Upstream's name-only key can return another bucket's bearer URL. Endpoint
    # also belongs to cache identity when two brands share Redis.
    identity = f'{blob.bucket._client.config.public_endpoint}\x00{blob.bucket.name}\x00{blob.name}\x00{minutes}'
    key = 'minio:v1:' + hashlib.sha256(identity.encode()).hexdigest()
    if cached := get_cached_signed_url(key):
        return cached
    result = blob.generate_signed_url(version='v4', expiration=timedelta(minutes=minutes), method='GET')
    cache_signed_url(key, result, minutes * 60)
    return result
