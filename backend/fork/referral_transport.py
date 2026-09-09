"""Referral HTTP adaptation to the admitted identity and public URL owners.

The upstream request models, auth dependencies, HMAC format, age policy and
transactional entitlement owner remain authoritative. Only the transport's
Firebase lookup and upstream-branded destinations are target-specific.
"""

from urllib.parse import urlencode

from fastapi import HTTPException, Response
from fastapi.responses import RedirectResponse

from . import auth_identity
from .profile import current


def _referrer(code):
    from utils.referrals import ReferralCodeError, referrer_uid_from_code

    try:
        return referrer_uid_from_code(code)
    except ReferralCodeError as error:
        raise HTTPException(404, detail='Referral link not found') from error


def get_referral_link(uid, response: Response):
    from routers import referrals as owner
    from utils.referrals import referral_link

    try:
        result = owner.ReferralLinkResponse(referral_url=referral_link(uid, public_base_url=current()['api_base_url']))
    except owner.ReferralCodeError as error:
        raise HTTPException(503, detail='Referral links are temporarily unavailable') from error
    response.headers['Cache-Control'] = 'no-store'
    owner.emit_posthog_event(uid, 'Referral Link Issued', {'program': owner.REFERRAL_PROGRAM})
    return result


def capture_referral(code):
    from routers import referrals as owner

    referrer_uid = _referrer(code)
    query = urlencode({'referral': code, 'environment': 'prod'})
    response = RedirectResponse(f"{current()['web_base_url'].rstrip('/')}/login?{query}", status_code=302)
    response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
    response.set_cookie(
        owner.REFERRAL_COOKIE_NAME,
        code,
        max_age=owner.REFERRAL_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=True,
        samesite='lax',
        path='/',
    )
    owner.emit_posthog_event(referrer_uid, 'Referral Link Captured', {'program': owner.REFERRAL_PROGRAM})
    return response


def claim_referral(body, uid):
    from routers import referrals as owner

    referrer_uid = _referrer(body.code)
    try:
        user = auth_identity.get_user(uid)
    except auth_identity.IdentityAuthorityUnavailable as error:
        raise HTTPException(503, detail={'code': 'auth_service_unavailable', 'retryable': True}) from error
    if user is None or user.disabled:
        raise HTTPException(401, detail='Invalid authorization token')
    claimed, reason = owner.claim_referral_trial(
        uid, referrer_uid, is_new_user=owner.is_new_referral_account(user.created_at_ms)
    )
    owner.emit_posthog_event(
        uid, 'Referral Claimed', {'program': owner.REFERRAL_PROGRAM, 'claimed': claimed, 'reason': reason}
    )
    return owner.ReferralClaimResponse(claimed=claimed, trial_days=owner.REFERRAL_TRIAL_DAYS)


def install(app):
    if current()['target'] != 'self_hosted':
        return
    expected = {
        'get_referral_link': get_referral_link,
        'capture_referral': capture_referral,
        'claim_referral': claim_referral,
    }
    found = set()
    for route in app.routes:
        endpoint = getattr(route, 'endpoint', None)
        name = getattr(endpoint, '__name__', '')
        if getattr(endpoint, '__module__', '') == 'routers.referrals' and name in expected:
            if name in found:
                raise RuntimeError('referral route owner is ambiguous')
            # Preserve upstream decoding/auth/response validation, as with the
            # speech transport. Response injection only adds cache protection.
            route.dependant.call = expected[name]
            if name == 'get_referral_link':
                route.dependant.response_param_name = 'response'
            found.add(name)
    if found != set(expected):
        raise RuntimeError('referral route owner drift')
