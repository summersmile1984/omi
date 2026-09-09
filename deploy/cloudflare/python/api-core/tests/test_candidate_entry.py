"""Execute the production ASGI composition, including its assertion middleware."""

import asyncio
from collections import Counter
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import entry
from internal_auth import create_request_context
from test_candidate_create import env, proposal
from test_candidate_routes import call


@pytest.fixture
def api(env):
    env.INTERNAL_ASSERTION_SECRET = 'production-composition-test-secret'

    async def send_job(message):
        env.APP_DB.published.append(message)

    env.JOBS = SimpleNamespace(send=send_job)

    async def application(scope, receive, send):
        await entry.app(dict(scope, env=env), receive, send)

    async def request(
        method, path, *, uid='owner', authority='better-auth', generation=0, key=None, request_headers=None, **kwargs
    ):
        headers = {'x-account-generation': str(generation)}
        if uid is not None:
            encoded, signature = create_request_context(
                uid,
                env.INTERNAL_ASSERTION_SECRET,
                audience='api-core',
                method=method,
                path=urlsplit(path).path,
                request_id='entry-test',
                authority=authority,
            )
            headers.update({'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature})
        headers.update(request_headers or {})
        if key is not None:
            headers['idempotency-key'] = key
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url='https://core.test'
        ) as client:
            return await client.request(method, path, headers=headers, **kwargs)

    return request


def test_production_route_registry_has_no_shadowed_method_paths():
    # Runtime composition guard. c66e9e3 mounted one router twice after import
    # alias shadowing; direct module tests did not see the missing owner.
    counts = Counter(
        (route.path, method) for route in entry.app.routes for method in (getattr(route, 'methods', None) or [])
    )
    assert {key: count for key, count in counts.items() if count > 1} == {}


def test_public_acceptance_reaches_internal_sync_and_original_integration_owner(api, env):
    candidate = call(api, 'POST', '/v1/candidates', key='entry-create', json=proposal().model_dump(mode='json'))
    assert candidate.status_code == 200, candidate.text
    identity = candidate.json()['candidate_id']
    accepted = call(api, 'POST', '/v1/candidates/' + identity + '/accept')
    assert accepted.status_code == 200, accepted.text
    task_id = accepted.json()['task_id']
    message = next(value for value in env.APP_DB.published if value['kind'] == 'candidate_integration')
    body = {
        'outbox_id': identity,
        'account_generation': 0,
        'lease_token': message['payload']['lease_token'],
        'platform': 'todoist',
    }
    prepared = call(
        api, 'POST', '/internal/candidates/integrations', authority='internal', json={'action': 'prepare', **body}
    )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()['task']['id'] == task_id
    settled = call(
        api,
        'POST',
        '/internal/candidates/integrations',
        authority='internal',
        json={'action': 'settle', 'succeeded': True, 'external_id': 'controlled-external', **body},
    )
    assert settled.status_code == 200 and settled.json() == {'status': 'completed'}, settled.text
    task = call(api, 'GET', '/v1/action-items/' + task_id)
    assert task.json()['exported'] and task.json()['export_platform'] == 'todoist'
    status = call(api, 'GET', '/v1/integrations/todoist')
    assert status.status_code == 200 and status.json() == {'connected': False, 'app_key': 'todoist'}


def test_actual_entry_requires_internal_authority_for_integration_delivery(api):
    response = call(
        api,
        'POST',
        '/internal/candidates/integrations',
        json={'action': 'schedule', 'outbox_id': 'not-owned', 'account_generation': 0},
    )
    assert response.status_code == 401, response.text
