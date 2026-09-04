"""Real upstream HTTP/WS consumers preserve fork authority failure semantics.

Database and Redis are isolated through the repository's sanctioned fixture;
verification outcomes enter through auth_shim, then the real Patch registry,
FastAPI dependencies, and ASGI HTTP/WebSocket routing execute unchanged.
"""

from __future__ import annotations

import importlib
import types
from unittest import mock

import pytest
from fastapi import Depends, FastAPI, WebSocket, WebSocketException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fork.auth_transport import install
from fork.patches.auth import patches
from fork.registry import build_registry
from testing.import_isolation import stub_modules


@pytest.fixture
def runtime(request):
    users = types.ModuleType('database.users')
    users.record_user_platform = mock.Mock()
    users.record_client_device = mock.Mock()
    users.get_user_deletion_wipe_status = mock.Mock(return_value=None)
    database_client = types.ModuleType('database._client')
    database_client.db = mock.MagicMock()
    database_client.document_id_from_seed = mock.Mock(return_value='synthetic')
    redis = types.ModuleType('database.redis_db')
    redis.check_rate_limit = mock.Mock(return_value=True)
    redis.try_acquire_listen_lock = mock.Mock(return_value=True)
    redis.try_acquire_user_platform_write_lock = mock.Mock(return_value=True)
    with stub_modules(
        {
            'database._client': database_client,
            'database.redis_db': redis,
            'database.users': users,
            'utils.other.endpoints': None,
        }
    ), mock.patch.dict('os.environ', {'AUTH_PROVIDER': 'better_auth', 'ADMIN_KEY_AUTH_ENABLED': 'false'}):
        endpoints = importlib.import_module('utils.other.endpoints')
        shim = importlib.import_module('utils.auth_shim')
        target = getattr(request, 'param', 'self_hosted')
        build_registry(patches()).apply({'target': target})
        app = FastAPI()
        if target == 'self_hosted':
            install(app)

        @app.get('/normal')
        def normal(uid: str = Depends(endpoints.get_current_user_uid)):
            return {'uid': uid}

        @app.get('/byok')
        def byok(uid: str = Depends(endpoints.get_current_user_uid_no_byok_validation)):
            return {'uid': uid}

        @app.websocket('/listen')
        async def listen(websocket: WebSocket, uid: str = Depends(endpoints.get_current_user_uid_ws_listen)):
            await websocket.accept()
            await websocket.send_json({'uid': uid})
            await websocket.close()

        @app.websocket('/web-listen')
        async def web_listen(websocket: WebSocket):
            await websocket.accept()
            try:
                await endpoints.get_current_user_uid_from_ws_message(await websocket.receive(), websocket=websocket)
            except WebSocketException as error:
                await websocket.close(code=error.code, reason=error.reason)

        with TestClient(app) as client:
            yield client, shim, endpoints, users


@pytest.mark.parametrize('outcome', ['authority-unavailable', 'invalid', 'expired', 'valid'])
def test_http_and_websocket_classify_authoritative_outcomes(runtime, outcome):
    client, shim, endpoints, users = runtime
    error = {
        'authority-unavailable': shim.CertificateFetchError('synthetic outage'),
        'invalid': shim.InvalidIdTokenError('synthetic invalid'),
    }.get(outcome)
    if outcome == 'expired':
        from jwt import ExpiredSignatureError

        error = shim.InvalidIdTokenError('signature expired')
        error.__cause__ = ExpiredSignatureError('expired')
    with mock.patch.object(shim, 'verify_id_token', side_effect=error, return_value={'uid': 'synthetic-user'}):
        for path in ('/normal', '/byok'):
            response = client.get(path, headers={'Authorization': 'Bearer synthetic'})
            assert (
                response.status_code
                == {'authority-unavailable': 503, 'invalid': 401, 'expired': 401, 'valid': 200}[outcome]
            )
            if outcome == 'authority-unavailable':
                assert response.json()['detail'] == {'code': 'auth_service_unavailable', 'retryable': True}
        if outcome == 'valid':
            with client.websocket_connect('/listen', headers={'Authorization': 'Bearer synthetic'}) as ws:
                assert ws.receive_json() == {'uid': 'synthetic-user'}
        else:
            with pytest.raises(WebSocketDisconnect) as closed:
                with client.websocket_connect('/listen', headers={'Authorization': 'Bearer synthetic'}) as ws:
                    ws.receive_json()
                    pytest.fail('unauthorized WebSocket received application data')
            assert closed.value.code == {'authority-unavailable': 1013, 'invalid': 1008, 'expired': 4001}[outcome]
            users.get_user_deletion_wipe_status.assert_not_called()


@pytest.mark.parametrize('runtime', ['omi_cloud'], indirect=True)
def test_upstream_legacy_profile_keeps_original_auth_consumers(runtime):
    client, shim, endpoints, _ = runtime
    with mock.patch.dict('os.environ', {'AUTH_PROVIDER': ''}), mock.patch.object(
        endpoints.auth, 'verify_id_token', return_value={'uid': 'legacy-user'}
    ), mock.patch.object(shim, 'verify_id_token', side_effect=AssertionError('legacy Firebase was rerouted')):
        response = client.get('/normal', headers={'Authorization': 'Bearer legacy'})
        assert response.status_code == 200
        assert response.json() == {'uid': 'legacy-user'}


@pytest.mark.parametrize('expired', [False, True])
def test_first_message_auth_keeps_retryable_close(runtime, expired):
    import json
    from jwt import ExpiredSignatureError

    client, shim, _, _ = runtime
    error = shim.CertificateFetchError('synthetic outage')
    if expired:
        error = shim.InvalidIdTokenError('expired')
        error.__cause__ = ExpiredSignatureError('expired')
    with mock.patch.object(shim, 'verify_id_token', side_effect=error):
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect('/web-listen') as websocket:
                websocket.send_text(json.dumps({'type': 'auth', 'token': 'synthetic'}))
                websocket.receive_json()
        assert closed.value.code == (4001 if expired else 1013)
