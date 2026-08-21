"""Same-origin identity bridge for browser OAuth consent pages."""

from __future__ import annotations

import hmac
import secrets
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException
from fastapi.responses import JSONResponse, Response

from utils.executors import critical_executor, run_blocking
from utils.identity import IdentityInvalidToken, IdentityProviderUnavailable, authenticate_email_to_jwt
from utils.other.endpoints import rate_limit_dependency

router = APIRouter()

IDENTITY_BROWSER_CSRF_COOKIE = 'omi_identity_csrf'


def new_browser_identity_csrf() -> str:
    return secrets.token_urlsafe(32)


def set_browser_identity_csrf(response: Response, token: str) -> None:
    response.set_cookie(
        IDENTITY_BROWSER_CSRF_COOKIE,
        token,
        max_age=600,
        httponly=True,
        secure=True,
        samesite='strict',
    )


@router.post('/v1/identity/email-token', dependencies=[Depends(rate_limit_dependency('identity-email', 10, 60))])
async def email_identity_token(
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    csrf_cookie: Optional[str] = Cookie(default=None, alias=IDENTITY_BROWSER_CSRF_COOKIE),
):
    if not csrf_cookie or not hmac.compare_digest(csrf_token, csrf_cookie):
        raise HTTPException(status_code=403, detail='This sign-in request is invalid or expired')
    try:
        token = await run_blocking(critical_executor, authenticate_email_to_jwt, email, password)
    except IdentityInvalidToken as exc:
        raise HTTPException(status_code=401, detail='Invalid email or password') from exc
    except IdentityProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail='Identity provider unavailable') from exc
    response = JSONResponse({'token': token})
    response.delete_cookie(IDENTITY_BROWSER_CSRF_COOKIE, secure=True, samesite='strict')
    return response
