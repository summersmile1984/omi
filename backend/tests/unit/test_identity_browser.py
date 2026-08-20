from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from routers import identity_browser


@pytest.mark.asyncio
async def test_email_identity_token_requires_double_submit_csrf():
    with pytest.raises(HTTPException) as exc_info:
        await identity_browser.email_identity_token(
            email='person@example.com',
            password='password',
            csrf_token='form-token',
            csrf_cookie='different-cookie',
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_email_identity_token_returns_provider_jwt_and_clears_cookie(monkeypatch):
    async def fake_run_blocking(_executor, fn, email, password):
        assert fn is identity_browser.authenticate_email_to_jwt
        assert (email, password) == ('person@example.com', 'password')
        return 'identity-jwt'

    monkeypatch.setattr(identity_browser, 'run_blocking', fake_run_blocking)

    response = await identity_browser.email_identity_token(
        email='person@example.com',
        password='password',
        csrf_token='same-token',
        csrf_cookie='same-token',
    )

    assert json.loads(response.body) == {'token': 'identity-jwt'}
    assert 'omi_identity_csrf=""' in response.headers['set-cookie']
