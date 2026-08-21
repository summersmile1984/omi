"""Unit tests for the MiMo-V2.5-TTS client and router provider selection.

Hermetic: never touches the real MiMo API. Covers request construction,
response parsing, provider selection, and error handling.
"""

from __future__ import annotations

import pytest

from utils.mimo_pipeline.tts import MimoTTSAPIError, MimoTTSClient


def test_client_requires_api_key():
    with pytest.raises(MimoTTSAPIError, match='MIMO_API_KEY'):
        MimoTTSClient(api_key='', base_url='http://operator.example.test/mimo')


def test_client_rejects_empty_text():
    client = MimoTTSClient(api_key='key', base_url='http://operator.example.test/mimo')
    with pytest.raises(MimoTTSAPIError, match='empty'):
        client.synthesize('   ')


def test_synthesize_builds_payload_and_decodes_audio(monkeypatch):
    captured = {}
    audio_bytes = b'RIFF\x24\x00\x00\x00WAVE'
    import base64

    class _FakeResp:
        is_success = True

        def json(self):
            return {
                'choices': [
                    {
                        'message': {
                            'audio': {
                                'data': base64.b64encode(audio_bytes).decode(),
                                'transcript': '你好',
                            }
                        }
                    }
                ]
            }

    import utils.mimo_pipeline.tts as mod

    def _post(url, headers, json, timeout):
        captured['url'] = url
        captured['payload'] = json
        return _FakeResp()

    monkeypatch.setattr(mod.httpx, 'post', _post)
    client = MimoTTSClient(api_key='test-key', base_url='http://operator.example.test/mimo', voice='冰糖')
    result = client.synthesize('你好', voice='茉莉')
    assert result == audio_bytes
    assert captured['payload']['model'] == 'mimo-v2.5-tts'
    assert captured['payload']['audio'] == {'format': 'wav', 'voice': '茉莉'}
    # assistant message carries the text
    assert captured['payload']['messages'][-1]['role'] == 'assistant'
    assert captured['payload']['messages'][-1]['content'] == '你好'


def test_synthesize_with_style_instruction(monkeypatch):
    captured = {}

    class _FakeResp:
        is_success = True

        def json(self):
            import base64

            return {'choices': [{'message': {'audio': {'data': base64.b64encode(b'x').decode()}}}]}

    import utils.mimo_pipeline.tts as mod

    def _post(url, headers, json, timeout):
        captured['payload'] = json
        return _FakeResp()

    monkeypatch.setattr(mod.httpx, 'post', _post)
    client = MimoTTSClient(api_key='k', base_url='http://operator.example.test/mimo')
    client.synthesize('text', style_instruction='用轻快语调')
    # style instruction goes in the user message
    assert captured['payload']['messages'][0] == {'role': 'user', 'content': '用轻快语调'}


def test_synthesize_raises_on_error_response(monkeypatch):
    import utils.mimo_pipeline.tts as mod

    class _ErrResp:
        is_success = False
        status_code = 401
        text = '{"error":{"message":"bad key"}}'

        def json(self):
            return {'error': {'message': 'bad key'}}

    monkeypatch.setattr(mod.httpx, 'post', lambda *a, **kw: _ErrResp())
    client = MimoTTSClient(api_key='wrong', base_url='http://operator.example.test/mimo')
    with pytest.raises(MimoTTSAPIError, match='bad key'):
        client.synthesize('你好')


def test_synthesize_raises_on_unexpected_shape(monkeypatch):
    import utils.mimo_pipeline.tts as mod

    class _FakeResp:
        is_success = True

        def json(self):
            return {'choices': []}

    monkeypatch.setattr(mod.httpx, 'post', lambda *a, **kw: _FakeResp())
    client = MimoTTSClient(api_key='k', base_url='http://operator.example.test/mimo')
    with pytest.raises(MimoTTSAPIError, match='unexpected'):
        client.synthesize('你好')


def test_mimo_enabled_flag(monkeypatch):
    import routers.tts as tts_mod

    monkeypatch.delenv('MIMO_API_KEY', raising=False)
    monkeypatch.delenv('TTS_PROVIDER', raising=False)
    assert tts_mod._is_mimo_enabled() is False

    monkeypatch.setenv('TTS_PROVIDER', 'mimo')
    monkeypatch.delenv('MIMO_API_KEY', raising=False)
    assert tts_mod._is_mimo_enabled() is False  # no key

    monkeypatch.setenv('MIMO_API_KEY', 'key')
    monkeypatch.setenv('MIMO_API_BASE', 'http://operator.example.test/mimo')
    assert tts_mod._is_mimo_enabled() is True

    monkeypatch.setenv('TTS_PROVIDER', 'elevenlabs')
    assert tts_mod._is_mimo_enabled() is False


def test_explicit_mimo_selection_does_not_fall_through_to_elevenlabs(monkeypatch):
    import routers.tts as tts_mod
    from fastapi import HTTPException
    from models.tts import TtsSynthesizeRequest

    monkeypatch.setenv('TTS_PROVIDER', 'mimo')
    monkeypatch.setenv('MIMO_API_KEY', 'key')
    monkeypatch.delenv('MIMO_API_BASE', raising=False)
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'must-not-be-used')

    with pytest.raises(HTTPException) as exc_info:
        import asyncio

        asyncio.run(tts_mod.tts_synthesize(TtsSynthesizeRequest(text='hello'), uid='user'))
    assert exc_info.value.status_code == 503
