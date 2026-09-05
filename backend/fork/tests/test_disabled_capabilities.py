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
from fork.patches.speech import patches as speech_patches


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
        ('/v2/messages', 'llm'),
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


def test_background_stt_is_not_empty_transcription_and_push_count_is_not_delivery(monkeypatch):
    from fork import profile

    monkeypatch.setattr(profile, 'current', lambda: {'target': 'self_hosted'})
    import utils.stt.pre_recorded as recorded
    import utils.notifications as notifications
    from firebase_admin import messaging

    with ExitStack() as stack:
        for patch in patches() + speech_patches():
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
    with pytest.raises(ValueError, match='admitted model bundle'):
        validate(
            {'capabilities': {'stt_providers': ['sensevoice'], 'tts_provider': 'disabled', 'push_provider': 'disabled'}}
        )


def test_selected_mimo_reaches_real_http_auth_while_push_stays_disabled():
    from fastapi import HTTPException
    from fork.operator_ai import configure

    app = FastAPI()
    for module in (
        'routers.tts',
        'routers.desktop_tts_updates',
        'routers.transcribe',
        'routers.chat',
        'routers.notifications',
    ):
        app.include_router(importlib.import_module(module).router)
    row = configure(
        {'target': 'self_hosted', 'stage': 'local', 'capabilities': {'push_provider': 'disabled'}}, 'mimo-cn'
    )
    install(app, row)

    def deny():
        raise HTTPException(401, 'controlled real-auth boundary')

    paths = {'/v2/messages', '/v2/tts/synthesize', '/v2/voice-message/transcribe'}
    for route in app.routes:
        if getattr(route, 'path', '') in paths:
            for dependency in route.dependant.dependencies:
                app.dependency_overrides[dependency.call] = deny
    with TestClient(app) as client:
        for path in paths:
            assert client.post(path, json={'text': 'existing client'}).status_code == 401
        assert client.post('/v1/users/fcm-token', json={'token': 'legacy'}).json()['capability'] == 'push'


def test_real_byok_error_entrypoints_cannot_bypass_disabled_delivery_or_set_cooldown(monkeypatch, caplog):
    import asyncio
    from utils import byok
    from utils.llm import byok_errors

    class RejectedKey(Exception):
        status_code = 401

    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    before_keys, before_uid = byok.get_byok_keys(), byok.get_byok_uid()
    before_validated = byok.has_validated_byok_keys()
    byok.set_validated_byok_keys({'openai': 'synthetic-existing-enrolled-key'}, 'existing-principal')
    try:
        # The production handler has no profile capability check: even with
        # self_hosted selected, an already-enrolled request reaches this owner.
        with mock.patch.object(byok_errors, '_send_byok_llm_error_notification') as reached:
            byok_errors.handle_llm_error(RejectedKey('synthetic failure'), 'openai')
            reached.assert_called_once_with('existing-principal', 'openai', 'invalid')
        with ExitStack() as stack:
            for patch in patches():
                module, original = patch.target()
                stack.enter_context(mock.patch.object(module, patch.attribute, patch.build(original)))
            attempts = [
                stack.enter_context(mock.patch.object(owner, name))
                for owner, name in (
                    (byok_errors.messaging, 'send_each'),
                    (byok_errors.notification_db, 'get_all_tokens'),
                    (byok_errors.notification_db, 'remove_bulk_tokens'),
                    (byok_errors, 'try_acquire_byok_llm_error_notification_lock'),
                    (byok_errors, 'release_byok_llm_error_notification_lock'),
                )
            ]
            byok_errors.handle_llm_error(RejectedKey('synthetic failure'), 'openai')
            asyncio.run(byok_errors.handle_llm_error_async(RejectedKey('synthetic failure'), 'openai'))
            assert byok_errors._send_byok_llm_error_notification('existing-principal', 'openai', 'invalid') is None
            for attempt in attempts:
                attempt.assert_not_called()
        events = [record.message for record in caplog.records if 'omi_fallback_event' in record.message]
        assert len(events) == 3
        assert all('component=pusher' in message and 'to=disabled' in message for message in events)
        assert not any(
            'BYOK LLM notification sent' in record.message or 'already sent recently' in record.message
            for record in caplog.records
        )
    finally:
        if before_validated:
            byok.set_validated_byok_keys(before_keys, before_uid)
        else:
            byok.set_byok_keys(before_keys)
            byok.set_byok_uid(before_uid)
