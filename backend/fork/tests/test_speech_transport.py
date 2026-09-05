"""Actual registered HTTP/WS consumers with controlled local inference seams."""

import asyncio
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
    monkeypatch.setattr(chat, 'try_reserve_session_budget', lambda *args: (True, 60000, 60000, 7140000))
    settled = mock.Mock(return_value=True)
    monkeypatch.setattr(chat, 'settle_reserved_duration', settled)

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
    settled.assert_called_once_with('existing-principal', 60000, 0 if failure else 100)


@pytest.fixture
def ptt_runtime(monkeypatch):
    from routers import chat

    monkeypatch.setattr(chat, 'is_trial_paywalled', lambda *args: False)
    monkeypatch.setattr(chat, 'get_effective_limit', lambda *args: (10, 60))
    monkeypatch.setattr(chat, 'check_rate_limit', lambda *args: (True, 9, 0))
    monkeypatch.setattr(chat, 'try_reserve_session_budget', lambda *args: (True, 60000, 60000, 7140000))
    settled = mock.Mock(return_value=True)
    monkeypatch.setattr(chat, 'settle_reserved_duration', settled)
    decoded = mock.Mock(return_value='controlled transcription')
    monkeypatch.setattr(local_socket, 'decode_pcm', decoded)
    sockets = []
    accepted = asyncio.Event()
    original_socket = local_socket.SenseVoiceSocket

    class TrackedSocket(original_socket):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, recognizer=object(), poll_seconds=0.001)
            sockets.append(self)

        def send(self, data):
            result = super().send(data)
            if result:
                accepted.set()
            return result

    monkeypatch.setattr(local_socket, 'SenseVoiceSocket', TrackedSocket)
    return SimpleNamespace(settled=settled, decoded=decoded, sockets=sockets, accepted=accepted)


class Session:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.closed = []
        self.sent = []

    async def accept(self):
        pass

    async def receive(self):
        message = next(self.messages)
        if isinstance(message, Exception):
            raise message
        return message

    async def close(self, code, reason):
        self.closed.append((code, reason))

    async def send_json(self, value):
        self.sent.append(value)


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_path', ['disconnect', 'disconnect_error', 'idle', 'limit', 'malformed', 'finalize'])
async def test_ptt_every_terminal_path_drains_and_charges_only_accepted_audio_once(ptt_runtime, exit_path):
    pcm = b'\x00\x01' * 1600
    terminal = {
        'disconnect': {'type': 'websocket.disconnect'},
        'disconnect_error': WebSocketDisconnect(1001),
        'idle': asyncio.TimeoutError(),
        'limit': {'type': 'websocket.receive', 'bytes': pcm * 600},
        'malformed': {'type': 'websocket.receive', 'bytes': b'odd'},
        'finalize': {'type': 'websocket.receive', 'text': 'finalize'},
    }[exit_path]
    ws = Session([{'type': 'websocket.receive', 'bytes': pcm}, terminal])
    await speech_transport.ptt(ws, 'existing-principal')
    ptt_runtime.settled.assert_called_once_with('existing-principal', 60000, 100)
    assert ptt_runtime.decoded.call_count == 1
    assert ptt_runtime.decoded.call_args.args[2] == pcm
    assert ptt_runtime.sockets[0]._pump_task.done()
    assert not ptt_runtime.sockets[0]._pcm
    if exit_path == 'finalize':
        assert ws.closed == [(1000, 'speech_finalized')]
    elif exit_path in ('idle', 'limit', 'malformed'):
        assert ws.closed[0][0] == 1008


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['decode', 'cancelled_pump', 'rejected', 'unaccepted'])
async def test_ptt_failed_or_unaccepted_audio_never_charges_on_disconnect(ptt_runtime, monkeypatch, failure):
    pcm = b'\x00\x01' * 1600
    frames = [{'type': 'websocket.receive', 'bytes': pcm}, {'type': 'websocket.disconnect'}]
    if failure == 'decode':
        ptt_runtime.decoded.side_effect = RuntimeError('controlled native fault')
    elif failure == 'cancelled_pump':

        async def cancelled_pump(self):
            raise asyncio.CancelledError

        monkeypatch.setattr(local_socket.SenseVoiceSocket, '_pump', cancelled_pump)
    elif failure == 'rejected':
        original_send = local_socket.SenseVoiceSocket.send
        sends = 0

        def reject_second(self, data):
            nonlocal sends
            sends += 1
            return False if sends == 2 else original_send(self, data)

        monkeypatch.setattr(local_socket.SenseVoiceSocket, 'send', reject_second)
        frames.insert(1, {'type': 'websocket.receive', 'bytes': pcm})
    else:
        frames[0]['bytes'] = b'odd'
    await speech_transport.ptt(Session(frames), 'existing-principal')
    ptt_runtime.settled.assert_called_once_with('existing-principal', 60000, 0)


@pytest.mark.asyncio
async def test_ptt_reservation_failure_falls_back_to_actual_duration_accounting(ptt_runtime, monkeypatch):
    from routers import chat

    fallback = mock.Mock()
    recorded = mock.Mock(return_value=True)
    monkeypatch.setattr(chat, 'try_reserve_session_budget', mock.Mock(side_effect=RuntimeError('redis unavailable')))
    monkeypatch.setattr(chat, 'record_actual_duration', recorded)
    monkeypatch.setattr(speech_transport, 'record_fallback', fallback)

    await speech_transport.ptt(
        Session(
            [
                {'type': 'websocket.receive', 'bytes': b'\x00\x01' * 1600},
                {'type': 'websocket.receive', 'text': 'finalize'},
            ]
        ),
        'existing-principal',
    )

    ptt_runtime.settled.assert_not_called()
    recorded.assert_called_once_with('existing-principal', 100)
    fallback.assert_called_once_with(
        component='ptt_cascade',
        from_mode='atomic_budget_reservation',
        to_mode='duration_settlement',
        reason='other',
        outcome='degraded',
    )


@pytest.mark.asyncio
async def test_cancelled_ptt_retains_healthy_tail_and_single_usage_owner(ptt_runtime, monkeypatch):
    waiting = asyncio.Event()
    decoding = asyncio.Event()
    release = asyncio.Event()
    original_run = local_socket.run_blocking

    async def held_decode(*args, **kwargs):
        decoding.set()
        await release.wait()
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(local_socket, 'run_blocking', held_decode)

    class CancelledSession(Session):
        async def receive(self):
            if not waiting.is_set():
                waiting.set()
                return {'type': 'websocket.receive', 'bytes': b'\x00\x01' * 1600}
            await asyncio.Future()

    task = asyncio.create_task(speech_transport.ptt(CancelledSession([]), 'existing-principal'))
    await ptt_runtime.accepted.wait()
    task.cancel()
    await decoding.wait()
    task.cancel()  # Cancellation during cleanup must not cancel the native tail.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    ptt_runtime.settled.assert_called_once_with('existing-principal', 60000, 100)
    assert ptt_runtime.decoded.call_count == 1
    assert ptt_runtime.sockets[0]._pump_task.done()


@pytest.mark.asyncio
async def test_cancelled_drain_waiter_cannot_drop_or_replay_socket_tail(ptt_runtime, monkeypatch):
    decoding = asyncio.Event()
    release = asyncio.Event()
    original_run = local_socket.run_blocking

    async def held_decode(*args, **kwargs):
        decoding.set()
        await release.wait()
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(local_socket, 'run_blocking', held_decode)
    socket = local_socket.SenseVoiceSocket()
    socket.start()
    assert socket.send(b'\x00\x01' * 1600)
    waiter = asyncio.create_task(socket.drain_and_close())
    await decoding.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not socket._pump_task.done()
    release.set()
    await socket.drain_and_close()
    await socket.drain_and_close()
    assert ptt_runtime.decoded.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('with_audio', [False, True])
async def test_ptt_idle_deadline_only_renews_for_accepted_audio(ptt_runtime, monkeypatch, with_audio):
    clock = [0]
    monkeypatch.setattr(speech_transport, 'monotonic', lambda: clock[0])
    frames = [
        (20, {'type': 'websocket.receive', 'bytes': b''}),
        (25, {'type': 'websocket.receive', 'text': 'keepalive'}),
        (31, {'type': 'websocket.receive', 'text': 'ignored'}),
    ]
    if with_audio:
        frames[1] = (25, {'type': 'websocket.receive', 'bytes': b'\x00\x01' * 1600})
        frames.extend([(45, {'type': 'websocket.receive', 'text': 'keepalive'}), (56, frames[2][1])])
    frames.append((60, {'type': 'websocket.receive', 'text': 'finalize'}))

    class TimedSession(Session):
        async def receive(self):
            timestamp, message = next(self.messages)
            clock[0] = timestamp
            return message

    ws = TimedSession(frames)
    await speech_transport.ptt(ws, 'existing-principal')
    assert ws.closed == [(1008, 'speech_audio_idle_timeout')]
    assert clock[0] == (56 if with_audio else 31)
    if with_audio:
        ptt_runtime.settled.assert_called_once_with('existing-principal', 60000, 100)
    else:
        ptt_runtime.settled.assert_called_once_with('existing-principal', 60000, 0)
