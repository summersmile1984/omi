"""Unit tests for the MiMo-V2.5-ASR pre-recorded STT provider.

Hermetic: never touches the real MiMo API. Covers provider selection,
PCM→WAV wrapping, socket lifecycle, and error handling.
"""

from __future__ import annotations

import pytest

from utils.mimo_pipeline.config import MimoConfigurationError, validate_mimo_base_url
from utils.mimo_pipeline.mimo_client import MimoAPIError, MimoClient, infer_audio_format
from utils.mimo_pipeline.config import mimo_is_configured
from utils.mimo_pipeline.socket import MimoSttSocket, pcm16_to_wav
from config.prerecorded_stt import PrerecordedSTTService
from config.stt_provider_policy import MIMO_PROVIDER, STTServingSurface, provider_is_enabled
from utils.egress_policy import EgressPolicyUnavailable
from utils.mimo_pipeline.prerecorded_provider import MimoPrerecordedProvider
from utils.stt.pre_recorded import get_prerecorded_service
from utils.stt.streaming import STTService, get_stt_service_for_language
import utils.mimo_pipeline.prerecorded_provider as prerecorded_module

OPERATOR_BASE = 'http://operator.example.test/mimo'


def test_mimo_is_batch_only():
    assert provider_is_enabled(MIMO_PROVIDER, STTServingSurface.PRERECORDED)
    assert not provider_is_enabled(MIMO_PROVIDER, STTServingSurface.STREAMING)


def test_enabled_only_with_operator_endpoint_and_key(monkeypatch):
    monkeypatch.delenv('MIMO_API_KEY', raising=False)
    monkeypatch.delenv('MIMO_API_BASE', raising=False)
    assert mimo_is_configured() is False
    monkeypatch.setenv('MIMO_API_KEY', 'key')
    assert mimo_is_configured() is False  # endpoint is required as well
    monkeypatch.setenv('MIMO_API_BASE', 'http://operator.example.test/mimo')
    assert mimo_is_configured() is True


def test_client_requires_explicit_operator_endpoint(monkeypatch):
    monkeypatch.delenv('MIMO_API_BASE', raising=False)
    monkeypatch.delenv('MIMO_TOKENPLAN_BASE', raising=False)
    monkeypatch.delenv('MIMO_USE_TOKENPLAN', raising=False)
    with pytest.raises(MimoAPIError, match='MIMO_API_BASE'):
        MimoClient(api_key='key')


@pytest.mark.parametrize(
    'base_url',
    [
        'https://api.xiaomimimo.com',
        'ftp://mimo.operator.example',
        'https://user:pass@mimo.operator.example',
        'https://mimo.operator.example?token=secret',
    ],
)
def test_client_rejects_vendor_or_unsafe_endpoint(base_url):
    with pytest.raises(MimoAPIError):
        MimoClient(api_key='key', base_url=base_url)


def test_client_resolves_explicit_tokenplan_endpoint(monkeypatch):
    monkeypatch.setenv('MIMO_USE_TOKENPLAN', 'true')
    monkeypatch.delenv('MIMO_API_BASE', raising=False)
    monkeypatch.setenv('MIMO_TOKENPLAN_BASE', OPERATOR_BASE + '/tokenplan')
    client = MimoClient(api_key='key')
    assert client._endpoint() == OPERATOR_BASE + '/tokenplan/v1/chat/completions'


def test_prerecorded_select_routes_to_mimo_when_configured(monkeypatch):
    monkeypatch.setenv('STT_PRERECORDED_MODEL', 'mimo')
    service, language, model = get_prerecorded_service('zh-CN')
    assert service == PrerecordedSTTService.MIMO
    assert language == 'zh'
    assert model == 'mimo-v2.5-asr'


def test_streaming_selection_rejects_batch_only_mimo(monkeypatch):
    import utils.stt.streaming as streaming

    monkeypatch.setenv('MIMO_API_KEY', 'key')
    monkeypatch.setenv('MIMO_API_BASE', 'http://operator.example.test/mimo')
    monkeypatch.delenv('SENSEVOICE_MODEL_DIR', raising=False)
    # stt_service_models is read at import time; patch the module-level list.
    monkeypatch.setattr(streaming, 'stt_service_models', ['mimo'])
    result = get_stt_service_for_language('zh-CN')
    assert result is not None
    service, _lang, model = result
    assert service in {STTService.modulate, STTService.parakeet, STTService.sensevoice}
    assert model != 'mimo'


def test_select_ignores_mimo_without_key(monkeypatch):
    import utils.stt.streaming as streaming

    monkeypatch.delenv('MIMO_API_KEY', raising=False)
    monkeypatch.setenv('MIMO_API_BASE', 'http://operator.example.test/mimo')
    monkeypatch.delenv('SENSEVOICE_MODEL_DIR', raising=False)
    monkeypatch.setattr(streaming, 'stt_service_models', ['mimo'])
    result = get_stt_service_for_language('zh-CN')
    # mimo without key must not be selected; falls to default policy
    assert result is None or result[0] in {STTService.modulate, STTService.parakeet, STTService.sensevoice}


def test_pcm16_to_wav_produces_valid_container():
    pcm = b'\x00\x00' * 8000  # 1s of silence at 16kHz/16bit
    wav = pcm16_to_wav(pcm, 16000, 1)
    assert wav[:4] == b'RIFF'
    assert wav[8:12] == b'WAVE'
    assert wav[20:22] == b'\x01\x00'  # PCM
    assert wav[22:24] == b'\x01\x00'  # mono


def test_socket_accumulates_and_calls_callback_on_finish(monkeypatch):
    captured = {}

    class _FakeClient:
        def transcribe_audio(self, audio, *, audio_format=None, language=None):
            captured['audio'] = audio
            captured['fmt'] = audio_format
            from utils.mimo_pipeline.mimo_client import MimoTranscription, MimoSegment

            return MimoTranscription(
                text='你好世界',
                duration=1.0,
                segments=[MimoSegment(0.0, 1.0, '你好世界', 'SPEAKER_00')],
            )

    def callback(segments):
        captured['segments'] = segments

    sock = MimoSttSocket(sample_rate=16000, transcript_callback=callback, client=_FakeClient())
    assert sock.send(b'\x00\x00' * 8000) is True
    sock.finish()
    assert captured['segments'] == [
        {
            'speaker': 'SPEAKER_00',
            'start': 0.0,
            'end': pytest.approx(0.5),
            'text': '你好世界',
            'is_user': False,
            'person_id': None,
        }
    ]
    assert captured['fmt'] == 'wav'
    assert captured['audio'][:4] == b'RIFF'
    assert sock.is_connection_dead is False


def test_socket_rejects_after_finish():
    sock = MimoSttSocket()
    assert sock.send(b'\x00\x00') is True
    sock.finish()
    assert sock.send(b'\x00\x00') is False  # finished


def test_socket_marks_dead_on_api_error(monkeypatch):
    class _ErrClient:
        def transcribe_audio(self, audio, **kw):
            raise MimoAPIError('bad key')

    sock = MimoSttSocket(transcript_callback=lambda *a: None, client=_ErrClient())
    sock.send(b'\x00\x00' * 16000)
    sock.finish()
    assert sock.is_connection_dead is True
    assert sock.death_reason == 'mimo_api:MimoAPIError'


def test_finalize_clears_buffer():
    sock = MimoSttSocket()
    sock.send(b'\x00\x00' * 100)
    sock.finalize()
    assert len(sock._pcm) == 0


def test_guess_format_by_suffix_and_content_type():
    assert infer_audio_format('audio.wav') == 'wav'
    assert infer_audio_format('audio.mp3') == 'mp3'
    assert infer_audio_format('x', 'audio/wav') == 'wav'
    assert infer_audio_format('x', 'audio/mpeg') == 'mp3'
    assert infer_audio_format('unknown.bin') == 'wav'  # conservative default


def test_client_requires_api_key():
    with pytest.raises(MimoAPIError, match='MIMO_API_KEY'):
        MimoClient(api_key='', base_url='http://operator.example.test')


def test_client_requires_explicit_endpoint(monkeypatch):
    monkeypatch.delenv('MIMO_API_BASE', raising=False)
    monkeypatch.setenv('MIMO_API_KEY', 'test-key')
    with pytest.raises(MimoAPIError, match='MIMO_API_BASE'):
        MimoClient()


@pytest.mark.parametrize(
    'endpoint',
    [
        'ftp://operator.example.test',
        'https://user:pass@operator.example.test',
        'https://operator.example.test/path?token=secret',
        'https://operator.example.test/path#fragment',
        'https://operator.example.test:bad',
    ],
)
def test_client_rejects_unsafe_endpoint(endpoint):
    with pytest.raises(MimoAPIError):
        MimoClient(api_key='test-key', base_url=endpoint)


def test_operator_endpoint_is_used_without_vendor_default():
    client = MimoClient(api_key='test-key', base_url='http://operator.example.test/mimo/')
    assert client._endpoint() == 'http://operator.example.test/mimo/v1/chat/completions'


def test_endpoint_validator_allows_private_operator_authority():
    assert validate_mimo_base_url('http://10.0.0.8:8080/mimo/') == 'http://10.0.0.8:8080/mimo'
    with pytest.raises(MimoConfigurationError):
        validate_mimo_base_url('https://operator.example.test?x=1')


@pytest.mark.parametrize('endpoint', ['https://api.xiaomimimo.com', 'https://token-plan-cn.xiaomimimo.com'])
def test_endpoint_validator_rejects_known_vendor_authority(endpoint):
    with pytest.raises(MimoConfigurationError, match='operator-owned'):
        validate_mimo_base_url(endpoint)


def test_transcribe_audio_builds_input_audio_and_parses_response(monkeypatch):
    captured = {}

    class _FakeResp:
        is_success = True

        def json(self):
            return {'choices': [{'message': {'content': '你好世界'}}]}

    import utils.mimo_pipeline.mimo_client as mod

    def _post(url, headers, json, timeout):
        captured['url'] = url
        captured['payload'] = json
        return _FakeResp()

    monkeypatch.setattr(mod.httpx, 'post', _post)
    client = MimoClient(api_key='test-key', base_url='http://operator.example.test')
    result = client.transcribe_audio(b'\x00\x01', audio_format='wav', language='zh')
    assert result.text == '你好世界'
    assert result.segments[0].text == '你好世界'
    assert result.segments[0].speaker == 'SPEAKER_00'
    msg_content = captured['payload']['messages'][0]['content']
    assert msg_content[0]['type'] == 'input_audio'
    # Official quick-start: data URL with MIME type, no separate format field
    assert msg_content[0]['input_audio']['data'].startswith('data:audio/wav;base64,')
    # Official: language via asr_options, not in content
    assert captured['payload']['asr_options'] == {'language': 'zh'}
    assert captured['payload']['model'] == 'mimo-v2.5-asr'
    assert captured['payload']['stream'] is False


def test_transcribe_audio_rejects_undeclared_neutral_endpoint_before_network(monkeypatch):
    """The sync MiMo client must not bypass neutral egress policy."""
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)
    import utils.mimo_pipeline.mimo_client as mod

    monkeypatch.setattr(mod.httpx, 'post', lambda *args, **kwargs: pytest.fail('network call'))
    client = MimoClient(api_key='test-key', base_url=OPERATOR_BASE)
    with pytest.raises(EgressPolicyUnavailable, match='egress_allowlist_not_configured'):
        client.transcribe_audio(b'\x00\x01')


def test_prerecorded_url_rejects_undeclared_audio_origin_before_download(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)
    monkeypatch.setattr(prerecorded_module.httpx, 'get', lambda *_args, **_kwargs: pytest.fail('network call'))

    provider = MimoPrerecordedProvider(client=object())
    with pytest.raises(EgressPolicyUnavailable, match='egress_allowlist_not_configured'):
        provider.transcribe_url('https://audio.operator.example/recording.wav')


def test_transcribe_audio_rejects_oversized(monkeypatch):
    monkeypatch.setattr('utils.mimo_pipeline.mimo_client.MAX_AUDIO_BYTES', 10)
    client = MimoClient(api_key='test-key', base_url='http://operator.example.test')
    with pytest.raises(MimoAPIError, match='too large'):
        client.transcribe_audio(b'\x00' * 100)


def test_transcribe_audio_raises_on_error_response(monkeypatch):
    import utils.mimo_pipeline.mimo_client as mod

    class _ErrResp:
        is_success = False
        status_code = 401
        text = '{"error":{"message":"bad key"}}'

        def json(self):
            return {'error': {'message': 'bad key'}}

    monkeypatch.setattr(mod.httpx, 'post', lambda *a, **kw: _ErrResp())
    client = MimoClient(api_key='wrong', base_url='http://operator.example.test')
    with pytest.raises(MimoAPIError, match='bad key'):
        client.transcribe_audio(b'\x00\x01')


def test_transcribe_audio_raises_on_unexpected_shape(monkeypatch):
    import utils.mimo_pipeline.mimo_client as mod

    class _FakeResp:
        is_success = True

        def json(self):
            return {'choices': []}

    monkeypatch.setattr(mod.httpx, 'post', lambda *a, **kw: _FakeResp())
    client = MimoClient(api_key='test-key', base_url='http://operator.example.test')
    with pytest.raises(MimoAPIError, match='unexpected'):
        client.transcribe_audio(b'\x00\x01')
