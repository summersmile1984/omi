#!/usr/bin/env python3
"""One HTTP product contract against either real, disposable deployment target.

This is the identity/onboarding/tasks slice of CI-1, not a release qualifier.
Target runners own disposable state, provider fixtures and teardown. This client
never seeds a database, imports a backend handler or supplies an auth bypass.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ContractFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ContractFailure(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials and exact wire statuses must not follow a redirected hop.
        return None


@dataclass(frozen=True)
class Principal:
    uid: str
    session: str
    jwt: str


class ProductContract:
    def __init__(self, metadata):
        require(metadata['target'] in {'self_hosted', 'cloudflare'}, 'unknown target')
        for key in ('api_origin', 'auth_origin'):
            value = urlsplit(metadata[key])
            require(
                value.scheme == 'http'
                and value.hostname in {'127.0.0.1', 'localhost'}
                and value.port
                and not value.username
                and not value.password
                and value.path in ('', '/')
                and not value.query
                and not value.fragment,
                f'{key} must be an explicit loopback fixture origin',
            )
        self.metadata = metadata
        self.trace_dir = Path(metadata['trace_dir']).resolve()
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.trace = self.trace_dir / 'core-http.jsonl'
        require(not self.trace.exists(), 'core trace already exists; use a fresh fixture')
        self.opener = build_opener(NoRedirect())
        self.cases = []

    def request(self, service, method, path, expected, *, bearer='', body=None):
        headers = {'Accept': 'application/json', 'X-App-Platform': 'web'}
        if service == 'auth':
            headers['Origin'] = self.metadata['auth_origin']
        if bearer:
            headers['Authorization'] = f'Bearer {bearer}'
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = Request(
            self.metadata[f'{service}_origin'].rstrip('/') + path,
            method=method,
            headers=headers,
            data=None if body is None else json.dumps(body).encode(),
        )
        start = time.monotonic()
        try:
            response = self.opener.open(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            raw = response.read(1024 * 1024 + 1)
            response_headers = {key.lower(): value for key, value in response.headers.items()}
        # Never store credentials, account identifiers, request/response bodies,
        # auth headers or free-form server errors in trace or assertion output.
        with self.trace.open('a') as output:
            output.write(
                json.dumps(
                    {
                        'service': service,
                        'method': method,
                        'route': '/v1/action-items/:id/completed' if '/completed?' in path else path,
                        'status': status,
                        'expected': expected,
                        'elapsed_ms': round((time.monotonic() - start) * 1000),
                    }
                )
                + '\n'
            )
        require(status == expected, f'{service} {method} returned HTTP {status}; expected {expected}')
        require(len(raw) <= 1024 * 1024, 'response exceeded contract limit')
        if not raw:
            return None, response_headers
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise ContractFailure('response is not JSON') from error
        return payload, response_headers

    def case(self, name, action):
        try:
            action()
        except Exception as error:
            self.cases.append(
                {
                    'id': name,
                    'result': 'fail',
                    'error': str(error) if isinstance(error, ContractFailure) else type(error).__name__,
                }
            )
            return False
        self.cases.append({'id': name, 'result': 'pass'})
        return True

    def signup(self):
        account = secrets.token_hex(12)
        body, headers = self.request(
            'auth',
            'POST',
            '/api/auth/sign-up/email',
            200,
            body={
                'email': f'product-{account}@example.invalid',
                'password': secrets.token_urlsafe(24),
                'name': 'Product Contract',
            },
        )
        uid = body.get('user', {}).get('id')
        session = headers.get('set-auth-token')
        require(isinstance(uid, str) and bool(uid), 'signup omitted identity')
        require(isinstance(session, str) and bool(session), 'signup omitted opaque session')
        require(session.count('.') != 2, 'durable credential is a JWT')
        # A new HTTP request, with no cookie jar or prior response state, restores
        # the account using only the credential clients persist in secure storage.
        restored, _ = self.request('auth', 'GET', '/api/auth/get-session', 200, bearer=session)
        require(restored.get('user', {}).get('id') == uid, 'restored identity differs')
        token, _ = self.request('auth', 'GET', '/api/auth/token', 200, bearer=session)
        jwt = token.get('token')
        require(isinstance(jwt, str) and jwt.count('.') == 2, 'token exchange omitted JWT')
        return Principal(uid, session, jwt)

    def run(self):
        principals = []

        def accounts():
            principals.extend((self.signup(), self.signup()))
            require(principals[0].uid != principals[1].uid, 'two signups share an identity')

        if not self.case('auth.signup-opaque-restore-jwt', accounts):
            return self.report()
        owner, other = principals

        def auth_admission():
            self.request('api', 'GET', '/v1/action-items', 401)
            self.request('api', 'GET', '/v1/action-items', 401, bearer='invalid-contract-token')
            self.request('api', 'GET', '/v1/action-items', 200, bearer=owner.jwt)

        self.case('auth.protected-admission', auth_admission)

        def onboarding():
            state, _ = self.request('api', 'GET', '/v1/users/onboarding', 200, bearer=owner.jwt)
            require(
                state == {'completed': False, 'acquisition_source': '', 'device_onboarding_completed': False},
                'new account onboarding differs',
            )
            repeated, _ = self.request('api', 'GET', '/v1/users/onboarding', 200, bearer=owner.jwt)
            require(repeated == state, 'repeated onboarding read changed public state')
            self.request(
                'api',
                'PATCH',
                '/v1/users/onboarding',
                200,
                bearer=owner.jwt,
                body={
                    'completed': True,
                    'acquisition_source': 'contract',
                    'device_onboarding_completed': True,
                },
            )
            restored, _ = self.request('api', 'GET', '/v1/users/onboarding', 200, bearer=owner.jwt)
            require(
                restored == {'completed': True, 'acquisition_source': 'contract', 'device_onboarding_completed': True},
                'onboarding mutation was not persisted',
            )
            isolated, _ = self.request('api', 'GET', '/v1/users/onboarding', 200, bearer=other.jwt)
            require(isolated['completed'] is False, 'onboarding crossed account boundary')

        self.case('onboarding.persist-and-isolate', onboarding)
        task = {}

        def create():
            created, _ = self.request(
                'api',
                'POST',
                '/v1/action-items',
                200,
                bearer=owner.jwt,
                body={'description': 'Disposable product contract task'},
            )
            require(isinstance(created.get('id'), str) and bool(created['id']), 'task omitted id')
            task.update(created)
            require(created.get('status') == 'active' and created.get('completed') is False, 'new task state differs')
            listing, _ = self.request('api', 'GET', '/v1/action-items', 200, bearer=owner.jwt)
            rows = [item for item in listing.get('action_items', []) if item.get('id') == task['id']]
            require(len(rows) == 1 and rows[0]['description'] == task['description'], 'created task was not persisted')

        created_ok = self.case('tasks.create-and-reread', create)
        if created_ok:
            task_path = '/v1/action-items/' + quote(task['id'], safe='') + '/completed?completed=true'

            def isolation():
                listing, _ = self.request('api', 'GET', '/v1/action-items', 200, bearer=other.jwt)
                require(
                    not any(item.get('id') == task['id'] for item in listing.get('action_items', [])),
                    'task visible to another account',
                )
                self.request('api', 'PATCH', task_path, 404, bearer=other.jwt)
                own, _ = self.request('api', 'GET', '/v1/action-items', 200, bearer=owner.jwt)
                require(
                    next(item for item in own['action_items'] if item['id'] == task['id'])['completed'] is False,
                    'denied write mutated owner task',
                )

            self.case('tasks.cross-account-denial', isolation)

            def completion():
                result, _ = self.request('api', 'PATCH', task_path, 200, bearer=owner.jwt)
                require(
                    result.get('status') == 'completed' and result.get('completed') is True,
                    'completion projections differ',
                )
                listing, _ = self.request('api', 'GET', '/v1/action-items?completed=true', 200, bearer=owner.jwt)
                require(
                    any(
                        item.get('id') == task['id'] and item.get('status') == 'completed'
                        for item in listing['action_items']
                    ),
                    'completion was not persisted',
                )

            self.case('tasks.complete-and-reread', completion)

        # Pinned by the actual FastAPI ActionItemCreateRequest min_length=1
        # boundary. CF returning 400 is a failing divergence, not an allowed case.
        self.case(
            'tasks.invalid-body-422',
            lambda: self.request(
                'api',
                'POST',
                '/v1/action-items',
                422,
                bearer=owner.jwt,
                body={'description': ''},
            ),
        )

        def refresh_logout():
            payload, _ = self.request('auth', 'GET', '/api/auth/token', 200, bearer=owner.session)
            refreshed = payload['token']
            self.request('api', 'GET', '/v1/action-items', 200, bearer=refreshed)
            self.request('auth', 'POST', '/api/auth/sign-out', 200, bearer=owner.session, body={})
            for token in (owner.jwt, refreshed):
                self.request('api', 'GET', '/v1/action-items', 401, bearer=token)
            self.request('api', 'GET', '/v1/action-items', 200, bearer=other.jwt)
            self.request('auth', 'POST', '/api/auth/sign-out', 200, bearer=other.session, body={})

        self.case('auth.refresh-logout-revocation', refresh_logout)
        return self.report()

    def report(self):
        report = {
            'schema_version': 1,
            'scope': 'identity-onboarding-tasks',
            'target': self.metadata['target'],
            'brand_id': self.metadata['brand_id'],
            'cases': self.cases,
            'passed': bool(self.cases) and all(case['result'] == 'pass' for case in self.cases),
            'release_qualified': False,
        }
        (self.trace_dir / 'core-results.json').write_text(json.dumps(report, indent=2) + '\n')
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata', required=True, type=Path)
    args = parser.parse_args()
    result = ProductContract(json.loads(args.metadata.read_text())).run()
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
