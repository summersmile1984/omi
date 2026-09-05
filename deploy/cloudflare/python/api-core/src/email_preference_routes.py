"""Scanner-safe lifecycle opt-out: signed link identity, Auth existence, App D1 consent."""

import base64
import hashlib
import hmac
import html
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from brand_runtime import load_brand_runtime
from internal_auth import create_request_context

router = APIRouter()
NEUTRAL_ERROR_HTML = (
    '<html><body><h1>This unsubscribe link is invalid or has expired.</h1>'
    '<p>If you followed a link from an email, please make sure you copied the whole address.</p>'
    '</body></html>'
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def verify_unsubscribe_token(token: str | None, secret: object) -> str | None:
    """Use upstream's canonical, purpose-bound, deliberately unexpiring HMAC format."""
    if not isinstance(secret, str) or not secret or not token or len(token) > 2048:
        return None
    try:
        encoded_uid, _ = token.split('.', 1)
        uid = base64.b64decode(encoded_uid + '=' * (-len(encoded_uid) % 4), altchars=b'-_', validate=True).decode(
            'utf-8'
        )
        if not uid:
            return None
        signature = hmac.new(secret.encode('utf-8'), f'{uid}:lifecycle'.encode('utf-8'), hashlib.sha256).digest()
        expected = f'{_b64url(uid.encode("utf-8"))}.{_b64url(signature)}'
        return uid if hmac.compare_digest(token.encode('ascii'), expected.encode('ascii')) else None
    except (ValueError, UnicodeError):
        return None


def _page(body: str, status: int = 200):
    return HTMLResponse(
        body,
        status_code=status,
        headers={
            'Cache-Control': 'no-store',
            'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        },
    )


async def _live_account(env, uid: str) -> bool:
    # Account existence stays with Better Auth. A valid years-old token must not
    # resurrect settings after the short-lived App deletion tombstone expires.
    signed = create_request_context(
        uid,
        getattr(env, 'INTERNAL_ASSERTION_SECRET', None),
        audience='auth',
        method='GET',
        path='/internal/profile',
        request_id='email-unsubscribe',
    )
    if signed is None or getattr(env, 'AUTH', None) is None:
        return False
    response = await env.AUTH.fetch(
        'https://auth.internal/internal/profile',
        method='GET',
        headers={'x-omi-auth-context': signed[0], 'x-omi-internal-signature': signed[1]},
    )
    if int(response.status) != 200:
        return False
    profile = await response.json()
    if not isinstance(profile, dict) or profile.get('uid') != uid:
        return False
    fence = (
        await env.APP_DB.prepare(
            'SELECT 1 AS active FROM cf_account_deletion_intents WHERE uid = ? '
            'UNION ALL SELECT 1 AS active FROM cf_account_deletion_tombstones WHERE uid = ? LIMIT 1'
        )
        .bind(uid, uid)
        .first()
    )
    return fence is None


async def _unsubscribe(request: Request, token: str | None, *, write: bool):
    env = request.scope['env']
    uid = verify_unsubscribe_token(token, getattr(env, 'LIFECYCLE_EMAIL_SIGNING_SECRET', None))
    if uid is None:
        return _page(NEUTRAL_ERROR_HTML, 400)
    try:
        name = html.escape(load_brand_runtime(env).display_name, quote=True)
        if not await _live_account(env, uid):
            return _page(NEUTRAL_ERROR_HTML, 400)
        if write:
            row = (
                await env.APP_DB.prepare(
                    'INSERT INTO cf_user_email_preferences (uid, lifecycle_opted_out, lifecycle_opted_out_at) '
                    'VALUES (?, 1, ?) ON CONFLICT(uid) DO UPDATE SET '
                    'lifecycle_opted_out = 1, lifecycle_opted_out_at = excluded.lifecycle_opted_out_at RETURNING uid'
                )
                .bind(uid, int(time.time()))
                .first()
            )
            if not row or row.get('uid') != uid:
                return _page(NEUTRAL_ERROR_HTML, 400)
            return _page(
                '<html><body><h1>You have been unsubscribed.</h1>'
                f'<p>You will no longer receive this kind of email from {name}.</p></body></html>'
            )
        safe_token = html.escape(token, quote=True)
        return _page(
            f'<html><body><h1>Unsubscribe from {name} emails?</h1>'
            '<p>You will stop receiving onboarding and re-engagement email. '
            'This does not affect email you ask for, or your account.</p>'
            f'<form method="post" action="/email/unsubscribe?token={safe_token}">'
            '<button type="submit">Unsubscribe</button></form></body></html>'
        )
    except Exception:
        # Never expose the token, UID, dependency error or account existence.
        return _page(NEUTRAL_ERROR_HTML, 400)


@router.get('/email/unsubscribe', response_class=HTMLResponse)
async def get_unsubscribe(request: Request, token: str | None = None):
    return await _unsubscribe(request, token, write=False)


@router.post('/email/unsubscribe', response_class=HTMLResponse)
async def post_unsubscribe(request: Request, token: str | None = None):
    # RFC 8058 body is intentionally ignored. Only the signed query token owns identity.
    return await _unsubscribe(request, token, write=True)
