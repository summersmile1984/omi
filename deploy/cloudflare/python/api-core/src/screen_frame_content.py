"""Read capabilities for the isolated screenshot writer; never write approvals."""

import asyncio
import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

router = APIRouter()
HEADERS = {'cache-control': 'no-store', 'x-content-type-options': 'nosniff'}
CONTENT_PATH = '/v1/screen-frame-content'


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def signing_secret(env) -> bytes:
    secret = getattr(env, 'SCREEN_FRAME_SIGNING_SECRET', None)
    internal = getattr(env, 'INTERNAL_ASSERTION_SECRET', None)
    if not isinstance(secret, str) or len(secret) < 32 or not internal or secret == internal:
        raise ValueError('isolated screenshot signing unavailable')
    return secret.encode()


def content_url(env, uid: str, frame_id: str, variant: str, access: str, expires_at: int) -> str:
    origin = getattr(env, 'PUBLIC_API_BASE_URL', '')
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {'https', 'http'}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {'', '/'}
        or parsed.query
        or parsed.fragment
        or any(ord(c) <= 32 or ord(c) == 127 for c in origin)
    ):
        raise ValueError('screenshot API origin unavailable')
    if variant not in {'content', 'thumbnail'} or access not in {'owner', 'shared'}:
        raise ValueError('invalid screenshot read scope')
    payload = {'uid': uid, 'frame_id': frame_id, 'variant': variant, 'access': access, 'expires_at': expires_at}
    message = 'screen-frame-content-v1.' + _encode(json.dumps(payload, separators=(',', ':')).encode())
    signature = _encode(hmac.new(signing_secret(env), message.encode(), hashlib.sha256).digest())
    return origin.rstrip('/') + CONTENT_PATH + '?' + urlencode({'token': message + '.' + signature})


async def _chunks(response):
    # Fetcher responses expose a JS ReadableStream in Pyodide. Release/cancel
    # on disconnect so the proxy never holds a complete image in Python memory.
    reader = response.body.getReader()
    done = False
    try:
        while True:
            result = await reader.read()
            if bool(result.done):
                done = True
                break
            to_py = getattr(result.value, 'to_py', None)
            yield bytes(to_py() if callable(to_py) else result.value)
    finally:
        try:
            if not done:
                await reader.cancel()
        finally:
            reader.releaseLock()


@router.get(CONTENT_PATH, include_in_schema=False)
async def screenshot_content(request: Request):
    token = request.query_params.get('token', '')
    if not token or len(token) > 4096 or not token.isascii():
        return Response(status_code=404, headers=HEADERS)
    env = request.scope['env']
    try:
        response = await asyncio.wait_for(
            env.SCREEN_FRAME_WRITER.fetch(
                'https://screen-frame-writer' + CONTENT_PATH + '?' + urlencode({'token': token})
            ),
            timeout=15,
        )
        if response.status != 200:
            return Response(status_code=404, headers=HEADERS)
    except Exception:
        return Response(status_code=503, headers=HEADERS)
    # The writer verifies the capability and rechecks current D1 privacy state
    # after fetching R2. Never forward caller cookies/headers or upstream headers.
    return StreamingResponse(_chunks(response), media_type='image/jpeg', headers=HEADERS)
