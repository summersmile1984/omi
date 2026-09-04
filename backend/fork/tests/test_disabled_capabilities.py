"""Actual upstream route registrations and provider consumers obey disabled policy."""

import importlib
from contextlib import ExitStack
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fork.capabilities import CapabilityDisabled, validate
from fork.capability_transport import install
from fork.patches.capabilities import patches


@pytest.fixture
def application():
    app = FastAPI()
    for module in (
        'routers.tts',
        'routers.desktop_tts_updates',
        'routers.transcribe',
        'routers.chat',
        'routers.notifications',
    ):
        app.include_router(importlib.import_module(module).router)
    install(app)
    return app


@pytest.mark.parametrize(
    'path,capability',
    [
        ('/v1/tts/synthesize', 'tts'),
        ('/v2/tts/synthesize', 'tts'),
        ('/v2/voice-messages', 'stt'),
        ('/v2/voice-message/transcribe', 'stt'),
        ('/v1/users/fcm-token', 'push'),
        ('/v1/notification', 'push'),
        ('/v1/integrations/notification', 'push'),
    ],
)
def test_real_http_owner_refuses_even_existing_vendor_credentials(application, monkeypatch, path, capability):
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'synthetic-existing-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-existing-key')
    with TestClient(application) as client:
        response = client.post(path, json={'text': 'synthetic', 'token': 'legacy-principal-token'})
    assert response.status_code == 503
    assert response.json() == {'code': 'deployment_capability_disabled', 'capability': capability, 'retryable': False}


@pytest.mark.parametrize('path', ['/v4/listen', '/v4/web/listen', '/v2/voice-message/transcribe-stream'])
def test_real_websocket_owner_closes_explicitly_without_accepting_audio(application, path):
    with TestClient(application) as client, client.websocket_connect(path) as socket:
        with pytest.raises(WebSocketDisconnect) as failure:
            socket.receive_text()
    assert failure.value.code == 1008
    assert failure.value.reason == 'stt_disabled'


def test_background_stt_is_not_empty_transcription_and_push_count_is_not_delivery():
    import utils.stt.pre_recorded as recorded
    import utils.notifications as notifications
    from firebase_admin import messaging

    with ExitStack() as stack:
        for patch in patches():
            module, original = patch.target()
            stack.enter_context(mock.patch.object(module, patch.attribute, patch.build(original)))
        network = stack.enter_context(
            mock.patch.object(messaging, 'send_each', side_effect=AssertionError('FCM called'))
        )
        telemetry = stack.enter_context(mock.patch('utils.observability.fallback.record_fallback'))
        with pytest.raises(CapabilityDisabled):
            recorded.prerecorded_from_bytes(b'not-sent', sample_rate=16000)
        assert notifications._send_to_user('existing-principal', 'tag', tokens=['old-fcm-token']) == 0
        assert (
            notifications.send_apple_reminders_sync_push('existing-principal', [{'id': 'task', 'description': 'test'}])
            is False
        )
        notifications.send_action_item_data_message('existing-principal', 'task', 'test', '2030-01-01T00:00:00Z')
        network.assert_not_called()
        assert telemetry.call_count >= 2


def test_admission_rejects_unsupported_enabled_capability():
    with pytest.raises(ValueError, match='no admitted provider'):
        validate(
            {'capabilities': {'stt_providers': ['sensevoice'], 'tts_provider': 'disabled', 'push_provider': 'disabled'}}
        )
