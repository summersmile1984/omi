"""Desktop referral wire contract with one D1 entitlement receipt per account."""

import base64
import hashlib
import hmac
import re
import uuid
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from account_routes import _auth_context, _account_created_at

router = APIRouter()
TRIAL_DAYS = 30
PROGRAM = 'desktop_operator_month_v1'
COOKIE_NAME = 'omi_desktop_referral'
UID_PATTERN = re.compile(r'^[A-Za-z0-9:_-]{1,128}$')


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _secret(env) -> bytes:
    value = getattr(env, 'REFERRAL_SIGNING_SECRET', None)
    if not isinstance(value, str) or len(value) < 32:
        raise ValueError('referral signing unavailable')
    return value.encode()


def _code(uid: str, env) -> str:
    if not UID_PATTERN.fullmatch(uid):
        raise ValueError('invalid referral identity')
    signed = 'ref1.' + _encode(uid.encode())
    signature = hmac.new(_secret(env), f'omi-desktop-referral:{signed}'.encode('ascii'), hashlib.sha256).digest()
    return signed + '.' + _encode(signature)


def _referrer(code: str, env) -> str:
    # Re-encoding enforces the upstream issuer's canonical representation.
    if not code or len(code) > 512:
        raise ValueError('invalid referral code')
    prefix, encoded, signature = code.split('.')
    uid = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True).decode()
    if prefix != 'ref1' or not hmac.compare_digest(code.encode('ascii'), _code(uid, env).encode('ascii')):
        raise ValueError('invalid referral code')
    return uid


def _origin(env, name: str) -> str:
    value = getattr(env, name, None)
    if not isinstance(value, str):
        raise ValueError('referral origin unavailable')
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {'https', 'http'}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {'', '/'}
        or parsed.query
        or parsed.fragment
        or any(ord(c) <= 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError('referral origin unavailable')
    return value.rstrip('/')


def _error(detail: str, status: int):
    return JSONResponse({'detail': detail}, status_code=status, headers={'cache-control': 'no-store'})


@router.get('/v1/users/me/referral')
async def referral_link(request: Request):
    context = _auth_context(request)
    if not context:
        return _error('unauthorized', 401)
    try:
        env = request.scope['env']
        url = _origin(env, 'PUBLIC_API_BASE_URL') + '/r/' + _code(str(context['uid']), env)
        return JSONResponse({'referral_url': url}, headers={'cache-control': 'no-store'})
    except Exception:
        return _error('Referral links are temporarily unavailable', 503)


@router.get('/r/{code}')
async def capture_referral(request: Request, code: str):
    env = request.scope['env']
    try:
        _referrer(code, env)
    except (ValueError, UnicodeError):
        return _error('Referral link not found', 404)
    try:
        url = _origin(env, 'PUBLIC_WEB_BASE_URL') + '/login?' + urlencode({'referral': code, 'environment': 'prod'})
    except Exception:
        return _error('Referral links are temporarily unavailable', 503)
    response = RedirectResponse(
        url, status_code=302, headers={'cache-control': 'no-store', 'referrer-policy': 'no-referrer'}
    )
    response.set_cookie(COOKIE_NAME, code, max_age=30 * 86400, httponly=True, secure=True, samesite='lax', path='/')
    return response


class ClaimRequest(BaseModel):
    code: str = Field(max_length=512)


@router.post('/v1/users/me/referral/claim')
async def claim_referral(request: Request):
    context = _auth_context(request)
    if not context:
        return _error('unauthorized', 401)
    env = request.scope['env']
    try:
        body = ClaimRequest.model_validate(await request.json())
    except (ValueError, ValidationError):
        return _error('Invalid referral claim', 422)
    try:
        referrer = _referrer(body.code, env)
    except (ValueError, UnicodeError):
        return _error('Referral link not found', 404)
    uid = str(context['uid'])
    created_at = _account_created_at(context)
    if created_at is None or uid == referrer:
        return {'claimed': False, 'trial_days': TRIAL_DAYS}
    claim_id = uuid.uuid4().hex
    try:
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    'INSERT INTO cf_referral_claims (uid, claim_id, program, claimed_at, trial_ends_at) '
                    "SELECT ?, ?, ?, unixepoch(), unixepoch() + 2592000 WHERE unixepoch() - ? BETWEEN 0 AND 900 "
                    "AND NOT EXISTS (SELECT 1 FROM cf_user_subscriptions WHERE uid = ? AND plan != 'basic') "
                    'ON CONFLICT(uid) DO NOTHING'
                ).bind(uid, claim_id, PROGRAM, created_at, uid),
                env.APP_DB.prepare(
                    'INSERT INTO cf_referral_attributions (uid, sender_uid) '
                    'SELECT uid, ? FROM cf_referral_claims WHERE uid = ? AND claim_id = ? '
                    'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) '
                    'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?) '
                    'ON CONFLICT(uid) DO NOTHING'
                ).bind(referrer, uid, claim_id, referrer, referrer),
            ]
        )
        row = await env.APP_DB.prepare('SELECT claim_id FROM cf_referral_claims WHERE uid = ?').bind(uid).first()
    except Exception:
        return _error('Referral claim temporarily unavailable', 503)
    return {'claimed': isinstance(row, dict) and row.get('claim_id') == claim_id, 'trial_days': TRIAL_DAYS}
