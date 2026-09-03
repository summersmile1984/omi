#!/usr/bin/env python3
"""Exercise Better Auth's native session -> JWT -> backend JWKS verifier flow."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import uuid
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request_json(
    base_url: str,
    path: str,
    *,
    method: str = 'GET',
    body: dict[str, str] | None = None,
    bearer: str = '',
    origin: str = '',
    client_ip: str = '',
) -> tuple[dict, dict[str, str]]:
    headers = {'Accept': 'application/json'}
    if origin:
        headers['Origin'] = origin
    if client_ip:
        headers['X-Forwarded-For'] = client_ip
    if bearer:
        headers['Authorization'] = f'Bearer {bearer}'
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode('utf-8')
    request = Request(f'{base_url.rstrip("/")}{path}', data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode('utf-8'))
            return payload, {name.lower(): value for name, value in response.headers.items()}
    except HTTPError as error:
        raw = error.read().decode('utf-8', errors='replace')
        try:
            detail = json.loads(raw).get('message') or json.loads(raw).get('code') or 'request rejected'
        except (AttributeError, json.JSONDecodeError):
            detail = 'request rejected'
        raise RuntimeError(f'{method} {path} failed with HTTP {error.code}: {detail}') from error


def require_session(payload: dict, headers: dict[str, str], *, expected_user: str = '') -> tuple[str, str]:
    user_id = str(payload.get('user', {}).get('id') or '')
    session_token = headers.get('set-auth-token', '').strip()
    if not user_id or not session_token:
        raise RuntimeError('Better Auth response omitted user.id or set-auth-token')
    if expected_user and user_id != expected_user:
        raise RuntimeError('Better Auth sign-in returned a different user')
    return user_id, session_token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True, help='direct URL used by this smoke client and JWKS fetch')
    parser.add_argument('--issuer', required=True, help='expected JWT issuer configured in Better Auth')
    parser.add_argument('--audience', required=True, help='expected JWT audience configured in Better Auth')
    parser.add_argument('--origin', required=True, help='trusted browser/client origin sent on auth requests')
    parser.add_argument(
        '--admin-secret',
        default=os.getenv('AUTH_INTERNAL_ADMIN_SECRET', ''),
        help='auth-server internal lifecycle bearer secret (prefer AUTH_INTERNAL_ADMIN_SECRET)',
    )
    parser.add_argument('--legacy-token-file', help='optional migrated legacy-JWK token fixture')
    args = parser.parse_args()
    if not args.admin_secret:
        parser.error('--admin-secret or AUTH_INTERNAL_ADMIN_SECRET is required')

    email = f'self-host-smoke-{uuid.uuid4().hex}@example.invalid'
    password = secrets.token_urlsafe(32)
    signup, signup_headers = request_json(
        args.base_url,
        '/api/auth/sign-up/email',
        method='POST',
        body={'name': 'Self Host Smoke', 'email': email, 'password': password},
        origin=args.origin,
        client_ip='192.0.2.1',
    )
    user_id, _ = require_session(signup, signup_headers)

    signin, signin_headers = request_json(
        args.base_url,
        '/api/auth/sign-in/email',
        method='POST',
        body={'email': email, 'password': password},
        origin=args.origin,
        client_ip='192.0.2.1',
    )
    _, session_token = require_session(signin, signin_headers, expected_user=user_id)

    token_payload, _ = request_json(
        args.base_url,
        '/api/auth/token',
        bearer=session_token,
        origin=args.origin,
        client_ip='192.0.2.1',
    )
    token = str(token_payload.get('token') or '')
    if token.count('.') != 2:
        raise RuntimeError('Better Auth token exchange did not return a JWT')

    jwks, _ = request_json(args.base_url, '/api/auth/jwks')
    if not isinstance(jwks.get('keys'), list) or not jwks['keys']:
        raise RuntimeError('Better Auth JWKS endpoint returned no keys')

    os.environ['AUTH_JWKS_URL'] = f'{args.base_url.rstrip("/")}/api/auth/jwks'
    os.environ['AUTH_JWT_ISSUER'] = args.issuer
    os.environ['AUTH_JWT_AUDIENCE'] = args.audience
    from utils.auth_shim import verify_id_token

    claims = verify_id_token(token)
    if claims.get('uid') != user_id or claims.get('sub') != user_id:
        raise RuntimeError('backend auth shim verified a JWT with the wrong subject')

    if args.legacy_token_file:
        legacy = json.loads(Path(args.legacy_token_file).read_text(encoding='utf-8'))
        legacy_claims = verify_id_token(str(legacy.get('token') or ''))
        if legacy_claims.get('uid') != 'legacy-jwks-user':
            raise RuntimeError('migrated legacy JWKS token did not verify through the backend')

    encoded_user_id = quote(user_id, safe='')
    request_json(
        args.base_url,
        f'/internal/users/{encoded_user_id}',
        method='DELETE',
        bearer=args.admin_secret,
    )
    residuals, _ = request_json(
        args.base_url,
        f'/internal/users/{encoded_user_id}/residuals',
        bearer=args.admin_secret,
    )
    if residuals != {'users': 0, 'sessions': 0, 'accounts': 0}:
        raise RuntimeError(f'Better Auth account deletion left residual rows: {residuals}')

    print(
        'Better Auth flow OK: sign-up, sign-in, set-auth-token, JWT exchange, JWKS, '
        'backend verification, account/session deletion reconciliation'
    )
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'ERROR: Better Auth flow smoke failed: {error}', file=sys.stderr)
        raise SystemExit(1)
