"""Actual registered HTTP/WS consumers with controlled local inference seams."""

import importlib
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fork import speech, speech_transport
from utils.sensevoice import socket as local_socket


@pytest.fixture
def application(monkeypatch):
    app = FastAPI()
    for name in ('routers.tts', 'routers.desktop_tts_updates', 'routers.chat'):
        app.include_router(importlib.import_module(name).router)
    speech_transport.install(app)
    return app


def authorize(application):
    for route in application.routes:
        if getattr(route, 'path', '') in (
            '/v1/tts/synthesize',
            '/v2/tts/synthesize',
            '/v2/voice-message/transcribe',
            '/v2/voice-message/transcribe-stream',
        ):
            for dependency in route.dependant.dependencies:
                application.dependency_overrides[dependency.call] = lambda: 'existing-principal'


def test_installed_tts_keeps_real_auth_dependency_and_no_inference_before_it(application, monkeypatch):
    calls = mock.Mock(side_effect=AssertionError('inference before auth'))
    monkeypatch.setattr(speech_transport, '_synthesize', calls)
    route = next(r for r in application.routes if getattr(r, 'path', '') == '/v2/tts/synthesize')

    def deny():
        raise HTTPException(401, 'invalid credential')

    application.dependency_overrides[route.dependant.dependencies[0].call] = deny
    with TestClient(application) as client:
        assert client.post('/v2/tts/synthesize', json={'text': 'hello'}).status_code == 401
    calls.assert_not_called()


@pytest.mark.parametrize('path', ['/v1/tts/synthesize', '/v2/tts/synthesize'])
def test_actual_tts_routes_return_audio_and_retryable_failure(application, monkeypatch, path):
    from database import redis_db

    authorize(application)
    monkeypatch.setattr(redis_db, 'check_tts_rate_limit', lambda *args, **kwargs: (0, 0))
    seen = []

    def audio(request):
        seen.append(speech_transport.voice_for_request(request))
        return b'controlled audio', 'audio/mpeg'

    monkeypatch.setattr(speech_transport, '_synthesize', audio)
    with TestClient(application) as client:
        response = client.post(path, json={'text': 'hello', 'voice_id': 'alloy'})
        assert response.status_code == 200 and response.content == b'controlled audio'
        assert response.headers['content-type'] == 'audio/mpeg'
        assert seen == ['af_heart']
        monkeypatch.setattr(
            speech_transport, '_synthesize', mock.Mock(side_effect=speech.SpeechError('speech_busy', retryable=True))
        )
        response = client.post(path, json={'text': 'hello', 'voice_id': 'alloy'})
        assert response.status_code == 503 and response.json()['detail']['retryable']
        monkeypatch.setattr(
            speech_transport, '_synthesize', mock.Mock(side_effect=speech.SpeechError('speech_model_store_required'))
        )
        response = client.post(path, json={'text': 'hello', 'voice_id': 'alloy'})
        assert response.status_code == 503 and response.json()['detail']['retryable'] is False
        monkeypatch.setattr(redis_db, 'check_tts_rate_limit', lambda *args, **kwargs: (-1, 0))
        assert client.post(path, json={'text': 'hello', 'voice_id': 'alloy'}).status_code == 503


def test_actual_http_selector_rejects_unsupported_language_as_typed_input(application, monkeypatch):
    from routers import chat

    authorize(application)
    monkeypatch.setattr(chat, 'is_trial_paywalled', lambda *args: False)
    monkeypatch.setattr(chat, 'resolve_voice_message_language', lambda *args: 'de')
    monkeypatch.setattr(chat, 'get_prerecorded_service', speech.prerecorded_selection)
    with TestClient(application) as client:
        result = client.post(
            '/v2/voice-message/transcribe',
            headers={'Content-Type': 'application/octet-stream'},
            content=b'\x00\x01' * 160,
        )
        assert result.status_code == 400
        assert result.json()['detail']['outcome'] == 'invalid_input'


@pytest.mark.parametrize('failure', [False, True])
def test_actual_ptt_wire_finalizes_real_socket_or_closes_failed(application, monkeypatch, failure):
    from routers import chat

    authorize(application)
    monkeypatch.setattr(chat, 'is_trial_paywalled', lambda *args: False)
    monkeypatch.setattr(chat, 'get_effective_limit', lambda *args: (10, 60))
    monkeypatch.setattr(chat, 'check_rate_limit', lambda *args: (True, 9, 0))
    monkeypatch.setattr(chat, 'check_budget', lambda *args: (True, 0, 60000))
    recorded = mock.Mock(return_value=True)
    monkeypatch.setattr(chat, 'record_actual_duration', recorded)

    def decode(*args):
        if failure:
            raise RuntimeError('controlled decode failure')
        return 'controlled transcription'

    monkeypatch.setattr(local_socket, 'get_sensevoice_recognizer', lambda: object())
    monkeypatch.setattr(local_socket, 'decode_pcm', decode)
    with TestClient(application) as client, client.websocket_connect('/v2/voice-message/transcribe-stream') as ws:
        ws.send_bytes(b'\x00\x01' * 1600)
        ws.send_text('finalize')
        result = ws.receive_json()
        if failure:
            assert result['status'] == 'stt_failed'
        else:
            assert result[0]['text'] == 'controlled transcription'
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_text()
        assert error.value.code == (1011 if failure else 1000)
    assert recorded.call_count == (0 if failure else 1)
