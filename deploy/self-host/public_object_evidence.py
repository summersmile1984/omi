#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Public signed-object acceptance seam shared by the live gate and hermetic tests."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit


def public_signed_object_crud(
    client: Any,
    *,
    objects_url: str,
    marker: str,
    storage_client: Any,
) -> dict[str, Any]:
    """Exercise signed S3 mutations through the reviewed public HTTPS origin."""

    bucket_name = os.getenv('BUCKET_TEMPORAL_SYNC_LOCAL', '').strip()
    if not bucket_name:
        raise RuntimeError('BUCKET_TEMPORAL_SYNC_LOCAL is required')
    blob = storage_client.bucket(bucket_name).blob(f'cutover-acceptance/{marker}.txt')
    payload = f'public-object-cutover:{marker}'.encode()
    expected_origin = urlsplit(objects_url)

    def require_signed_origin(url: str, operation: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or (parsed.scheme, parsed.netloc) != (
            expected_origin.scheme,
            expected_origin.netloc,
        ):
            raise RuntimeError(f'{operation} signed URL did not use PUBLIC_OBJECTS_URL')

    try:
        put_url = blob.generate_signed_url(expiration=300, method='PUT')
        require_signed_origin(put_url, 'PUT')
        put_response = client.put(put_url, content=payload, headers={'Content-Type': 'application/octet-stream'})
        if put_response.status_code >= 300:
            raise RuntimeError(f'public signed object PUT returned HTTP {put_response.status_code}')

        get_url = blob.generate_signed_url(expiration=300, method='GET')
        require_signed_origin(get_url, 'GET')
        get_response = client.get(get_url)
        if get_response.status_code != 200 or get_response.content != payload:
            raise RuntimeError('public signed object GET did not return the uploaded payload')

        delete_url = blob.generate_signed_url(expiration=300, method='DELETE')
        require_signed_origin(delete_url, 'DELETE')
        delete_response = client.delete(delete_url)
        if delete_response.status_code >= 300:
            raise RuntimeError(f'public signed object DELETE returned HTTP {delete_response.status_code}')
        if blob.exists():
            raise RuntimeError('public signed object DELETE left the object present in authoritative storage')
    finally:
        if blob.exists():
            blob.delete()

    return {
        'status': 'passed',
        'public_https_origin_exact': True,
        'signed_put': True,
        'signed_get_payload_match': True,
        'signed_delete_authoritative_absence': True,
    }
