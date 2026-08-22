import httpx
import pytest

from utils.moss_pipeline.config import (
    MossConfigurationError,
    resolve_moss_config,
    validate_moss_audio_url,
    validate_moss_base_url,
)
from utils.moss_pipeline.moss_client import MlxAudioClient, MossAPIError, download_audio_url
from utils.stt.pre_recorded import get_prerecorded_provider


def test_moss_base_requires_explicit_operator_authority_and_blocks_vendor_ssrf():
    with pytest.raises(MossConfigurationError, match='required'):
        validate_moss_base_url('')
    with pytest.raises(MossConfigurationError, match='managed MOSS'):
        validate_moss_base_url('https://api.mosi.cn')
    with pytest.raises(MossConfigurationError, match='blocked'):
        validate_moss_base_url('http://169.254.169.254')
    assert validate_moss_base_url('http://127.0.0.1:5002') == 'http://127.0.0.1:5002'


def test_moss_audio_url_requires_allowlist_for_private_authority(monkeypatch):
    monkeypatch.delenv('MOSS_AUDIO_URL_ALLOWLIST', raising=False)
    with pytest.raises(MossConfigurationError, match='allowlist'):
        validate_moss_audio_url('http://127.0.0.1:9000/audio.wav')
    with pytest.raises(MossConfigurationError, match='managed MOSS'):
        validate_moss_audio_url('https://api.mosi.cn/audio.wav')
    with pytest.raises(MossConfigurationError, match='blocked'):
        validate_moss_audio_url('http://169.254.169.254/latest/meta-data')
    monkeypatch.setenv('MOSS_AUDIO_URL_ALLOWLIST', '127.0.0.1')
    assert validate_moss_audio_url('http://127.0.0.1:9000/audio.wav?sig=one') == (
        'http://127.0.0.1:9000/audio.wav?sig=one'
    )


def test_mlx_config_allows_keyless_operator_and_requires_model(monkeypatch):
    monkeypatch.delenv('MOSS_API_KEY', raising=False)
    config = resolve_moss_config(
        transport='mlx_audio',
        base_url='http://127.0.0.1:5002',
        model='kuotient/MOSS-Transcribe-Diarize-MLX-8bit',
    )
    assert config.api_key == ''
    assert config.model.endswith('MLX-8bit')
    with pytest.raises(MossConfigurationError, match='MOSS_MODEL'):
        resolve_moss_config(transport='mlx_audio', base_url='http://127.0.0.1:5002')


def test_mlx_client_uses_models_and_multipart_transcription_wire():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == '/v1/models':
            return httpx.Response(
                200,
                json={'object': 'list', 'data': [{'id': 'kuotient/MOSS-Transcribe-Diarize-MLX-8bit'}]},
                request=request,
            )
        assert request.url.path == '/v1/audio/transcriptions'
        body = request.read()
        assert b'kuotient/MOSS-Transcribe-Diarize-MLX-8bit' in body
        assert b'response_format' in body
        return httpx.Response(
            200,
            json={
                'text': 'hello',
                'duration': 1.25,
                'segments': [{'start': 0.0, 'end': 1.25, 'text': 'hello', 'speaker': 'S01'}],
            },
            request=request,
        )

    client = MlxAudioClient(
        base_url='http://127.0.0.1:5002',
        model='kuotient/MOSS-Transcribe-Diarize-MLX-8bit',
    )
    client._client.close()
    client._client = httpx.Client(
        base_url='http://127.0.0.1:5002',
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    try:
        assert client.list_models() == ['kuotient/MOSS-Transcribe-Diarize-MLX-8bit']
        result = client.transcribe_audio(b'RIFF', filename='clip.wav', diarize=True)
    finally:
        client.close()
    assert result.text == 'hello'
    assert result.segments[0].speaker == 'S01'
    assert [request.url.path for request in requests] == ['/v1/models', '/v1/audio/transcriptions']


def test_mlx_audio_url_download_is_guarded_before_http_transport(monkeypatch):
    class UnexpectedTransport:
        def __init__(self, *args, **kwargs):
            raise AssertionError('HTTP transport must not be constructed for blocked URL')

    monkeypatch.setattr('utils.moss_pipeline.moss_client.httpx.Client', UnexpectedTransport)
    with pytest.raises(MossAPIError, match='blocked|allowlist'):
        download_audio_url('http://127.0.0.1:5002/recording.wav')


def test_explicit_moss_selection_does_not_fall_back_when_configuration_is_missing(monkeypatch):
    monkeypatch.setenv('STT_PRERECORDED_MODEL', 'moss')
    for name in ('MOSS_API_BASE', 'MOSS_API_KEY', 'MOSS_MODEL', 'MOSS_TRANSPORT'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(MossConfigurationError, match='MOSS_API_(BASE|KEY)'):
        get_prerecorded_provider()
