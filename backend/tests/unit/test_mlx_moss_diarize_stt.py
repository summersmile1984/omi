from __future__ import annotations

import json
import wave
from io import BytesIO

import httpx
import numpy as np
import pytest

from config.prerecorded_stt import (
    PrerecordedSTTConfigurationError,
    PrerecordedSTTService,
    get_mlx_moss_diarize_config,
    required_env_for_model_config,
)
from utils.mlx_moss_diarize import prerecorded_provider as mlx_provider
from utils.mlx_moss_diarize.prerecorded_provider import MlxMossDiarizePrerecordedProvider
from utils import http_client
from utils.stt import pre_recorded
from utils.stt.outcomes import TranscriptionFailure

MODEL = 'kuotient/MOSS-Transcribe-Diarize-MLX-8bit'
ENDPOINT = 'http://127.0.0.1:5002/v1/audio/transcriptions'


@pytest.fixture(autouse=True)
def _reset_provider_circuit() -> None:
    http_client._webhook_circuit_breakers.clear()


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('STT_PRERECORDED_MODEL', 'mlx_moss_diarize')
    monkeypatch.setenv('MLX_MOSS_DIARIZE_ENDPOINT', ENDPOINT)
    monkeypatch.setenv('MLX_MOSS_DIARIZE_MODEL', MODEL)
    monkeypatch.delenv('MLX_MOSS_DIARIZE_API_KEY', raising=False)


def _wav(seconds: float = 1.0) -> bytes:
    output = BytesIO()
    with wave.open(output, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b'\x00\x00' * int(16000 * seconds))
    return output.getvalue()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_success_uses_explicit_multipart_wire_and_normalizes_speakers(monkeypatch) -> None:
    _configure(monkeypatch)
    captured = {}
    fallback_events = []
    monkeypatch.setattr(mlx_provider, 'record_fallback', lambda **fields: fallback_events.append(fields))

    def handler(request: httpx.Request) -> httpx.Response:
        captured['url'] = str(request.url)
        captured['authorization'] = request.headers.get('authorization')
        captured['content_type'] = request.headers['content-type']
        captured['body'] = request.content
        return httpx.Response(
            200,
            json={
                'segments': [
                    {'start': 0.07, 'end': 0.45, 'speaker_id': 'S01', 'text': '[S01] 第一位说话人'},
                    {'start': 0.50, 'end': 0.90, 'speaker_id': 'S02', 'text': '[S02] 第二位说话人'},
                ],
                'total_time': 9.588,
            },
        )

    provider = MlxMossDiarizePrerecordedProvider(client=_client(handler))
    words = provider.transcribe_bytes(
        _wav(),
        encoding='audio/wav',
        language='zh-CN',
        keywords=['Omi', 'MOSS', 'Omi'],
    )

    assert words == [
        {'timestamp': [0.07, 0.45], 'speaker': 'SPEAKER_01', 'text': '第一位说话人'},
        {'timestamp': [0.5, 0.9], 'speaker': 'SPEAKER_02', 'text': '第二位说话人'},
    ]
    assert captured['url'] == ENDPOINT
    assert captured['authorization'] is None
    assert captured['content_type'].startswith('multipart/form-data; boundary=')
    assert b'name="file"; filename="chunk-0000.wav"' in captured['body']
    assert b'name="model"' in captured['body'] and MODEL.encode() in captured['body']
    assert b'name="response_format"' in captured['body'] and b'verbose_json' in captured['body']
    assert b'name="max_tokens"' in captured['body'] and str(mlx_provider.MAX_TOKENS).encode() in captured['body']
    assert b'name="context"' not in captured['body']
    assert b'name="language"' in captured['body'] and b'zh' in captured['body']
    assert fallback_events == [
        {
            'component': 'stt_selection',
            'from_mode': 'mlx_moss_diarization_with_context',
            'to_mode': 'mlx_moss_diarization_without_context',
            'reason': 'capability_mismatch',
            'outcome': 'degraded',
        }
    ]


def test_missing_config_fails_before_client_construction(monkeypatch) -> None:
    monkeypatch.setenv('STT_PRERECORDED_MODEL', 'mlx_moss_diarize')
    monkeypatch.delenv('MLX_MOSS_DIARIZE_ENDPOINT', raising=False)
    monkeypatch.setenv('MLX_MOSS_DIARIZE_MODEL', MODEL)
    monkeypatch.setattr(mlx_provider, 'get_stt_client', lambda: pytest.fail('client must stay lazy'))

    with pytest.raises(PrerecordedSTTConfigurationError) as raised:
        pre_recorded.get_prerecorded_provider('zh')

    assert raised.value.provider == PrerecordedSTTService.MLX_MOSS_DIARIZE
    assert raised.value.missing_env == 'MLX_MOSS_DIARIZE_ENDPOINT'


@pytest.mark.parametrize(
    ('payload', 'expected_outcome'),
    [
        ({'segments': 'not-a-list'}, 'upstream_error'),
        ({'segments': [{'start': 0.0, 'end': 0.5, 'text': 'no speaker'}]}, 'upstream_error'),
        (
            {'segments': [{'start': 0.0, 'end': 2.0, 'speaker_id': 'S01', 'text': '[S01] out of bounds'}]},
            'upstream_error',
        ),
        (
            {'segments': [{'start': 0.0, 'end': 0.5, 'speaker_id': 'speaker-one', 'text': 'bad label'}]},
            'upstream_error',
        ),
        (
            {
                'segments': [
                    {'start': 0.0, 'end': 0.9, 'speaker_id': 'S01', 'text': '[S01] first'},
                    {'start': 0.5, 'end': 0.8, 'speaker_id': 'S02', 'text': '[S02] overlap'},
                ]
            },
            'upstream_error',
        ),
    ],
)
def test_invalid_response_is_a_typed_failure(monkeypatch, payload, expected_outcome) -> None:
    _configure(monkeypatch)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider = MlxMossDiarizePrerecordedProvider(client=_client(handler))
    with pytest.raises(TranscriptionFailure) as raised:
        provider.transcribe_bytes(_wav(), encoding='audio/wav')

    assert raised.value.outcome.value == expected_outcome
    assert raised.value.provider == PrerecordedSTTService.MLX_MOSS_DIARIZE


def test_response_byte_and_segment_limits_fail_closed(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(mlx_provider, 'MAX_RESPONSE_BYTES', 80)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({'segments': []}).encode() + b' ' * 100)

    provider = MlxMossDiarizePrerecordedProvider(client=_client(handler))
    with pytest.raises(TranscriptionFailure) as raised:
        provider.transcribe_bytes(_wav(), encoding='audio/wav')

    assert raised.value.outcome.value == 'upstream_error'


def test_oversize_audio_fails_before_client_construction(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(mlx_provider, 'MAX_AUDIO_BYTES', 3)
    monkeypatch.setattr(
        mlx_provider, 'get_stt_client', lambda: pytest.fail('oversize input must not construct a client')
    )
    provider = MlxMossDiarizePrerecordedProvider()

    with pytest.raises(TranscriptionFailure) as raised:
        provider.transcribe_bytes(b'four', encoding='linear16')

    assert raised.value.outcome.value == 'invalid_input'
    assert raised.value.retryable is False


def test_long_audio_chunks_reconcile_request_local_speakers_with_embeddings(monkeypatch) -> None:
    _configure(monkeypatch)
    assert mlx_provider.REQUEST_CHUNK_DURATION_SECONDS == 240
    monkeypatch.setattr(mlx_provider, 'REQUEST_CHUNK_DURATION_SECONDS', 1)
    monkeypatch.setattr(mlx_provider, 'validate_speaker_embedding_configuration', lambda: 'sherpa_onnx')
    request_count = 0
    embeddings = iter(
        (
            np.array([[1.0, 0.0]], dtype=np.float32),
            np.array([[0.0, 1.0]], dtype=np.float32),
            np.array([[1.0, 0.0]], dtype=np.float32),
            np.array([[0.0, 1.0]], dtype=np.float32),
        )
    )

    async def fake_embedding(_audio: bytes, filename: str):
        assert filename.startswith('mlx-moss-speaker-')
        return next(embeddings)

    monkeypatch.setattr(mlx_provider, 'async_extract_embedding_from_bytes', fake_embedding)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                'segments': [
                    {'start': 0.0, 'end': 0.6, 'speaker_id': 'S01', 'text': '[S01] hello'},
                    {'start': 0.4, 'end': 0.9, 'speaker_id': 'S02', 'text': '[S02] world'},
                ]
            },
        )

    provider = MlxMossDiarizePrerecordedProvider(client=_client(handler))
    words = provider.transcribe_bytes(_wav(1.9), encoding='audio/wav')

    assert request_count == 2
    assert [word['timestamp'] for word in words] == [[0.0, 0.6], [0.4, 0.9], [1.0, 1.6], [1.4, 1.9]]
    assert [word['speaker'] for word in words] == ['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_00', 'SPEAKER_01']


def test_long_audio_fails_before_stt_client_when_local_embedding_is_unavailable(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(mlx_provider, 'REQUEST_CHUNK_DURATION_SECONDS', 1)
    monkeypatch.setattr(
        mlx_provider,
        'validate_speaker_embedding_configuration',
        lambda: (_ for _ in ()).throw(mlx_provider.SpeakerEmbeddingUnavailable('missing model')),
    )
    monkeypatch.setattr(mlx_provider, 'get_stt_client', lambda: pytest.fail('STT client must stay lazy'))

    with pytest.raises(TranscriptionFailure) as raised:
        MlxMossDiarizePrerecordedProvider().transcribe_bytes(_wav(1.1), encoding='audio/wav')

    assert raised.value.outcome.value == 'config_error'
    assert raised.value.retryable is False


def test_long_nondiarized_audio_needs_no_embedding_and_maps_to_single_speaker(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(mlx_provider, 'REQUEST_CHUNK_DURATION_SECONDS', 1)
    monkeypatch.setattr(
        mlx_provider,
        'validate_speaker_embedding_configuration',
        lambda: pytest.fail('diarize=false must not require speaker embeddings'),
    )
    request_count = 0
    request_bodies = []
    monkeypatch.setattr(
        mlx_provider,
        'record_fallback',
        lambda **_fields: pytest.fail('diarize=false must not suppress context'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        request_bodies.append(request.content)
        return httpx.Response(200, json={'segments': [{'start': 0.0, 'end': 0.5, 'text': 'plain transcript'}]})

    words = MlxMossDiarizePrerecordedProvider(client=_client(handler)).transcribe_bytes(
        _wav(1.5),
        encoding='audio/wav',
        diarize=False,
        keywords=['Omi', 'MOSS', 'Omi'],
    )

    assert request_count == 2
    assert [word['speaker'] for word in words] == ['SPEAKER_00', 'SPEAKER_00']
    assert [word['timestamp'] for word in words] == [[0.0, 0.5], [1.0, 1.5]]
    assert all(b'name="context"' in body and b'Omi,MOSS' in body for body in request_bodies)


def test_production_selector_never_constructs_hosted_moss_client(monkeypatch) -> None:
    _configure(monkeypatch)
    from utils.moss_pipeline import moss_client

    monkeypatch.setattr(moss_client, 'MossClient', lambda *args, **kwargs: pytest.fail('hosted client constructed'))

    service, language, model = pre_recorded.get_prerecorded_service('zh-CN')
    provider = pre_recorded.get_prerecorded_provider('zh-CN')

    assert (service, language, model) == (PrerecordedSTTService.MLX_MOSS_DIARIZE, 'zh', MODEL)
    assert isinstance(provider, MlxMossDiarizePrerecordedProvider)


def test_endpoint_policy_allows_private_http_and_requires_bearer_for_public_https() -> None:
    assert required_env_for_model_config('mlx_moss_diarize') == (
        'MLX_MOSS_DIARIZE_ENDPOINT',
        'MLX_MOSS_DIARIZE_MODEL',
    )
    assert (
        get_mlx_moss_diarize_config(
            {
                'MLX_MOSS_DIARIZE_ENDPOINT': 'http://host.docker.internal:5002/v1/audio/transcriptions',
                'MLX_MOSS_DIARIZE_MODEL': MODEL,
            }
        ).api_key
        is None
    )
    assert (
        get_mlx_moss_diarize_config(
            {
                'MLX_MOSS_DIARIZE_ENDPOINT': 'http://[::1]:5002/v1/audio/transcriptions',
                'MLX_MOSS_DIARIZE_MODEL': MODEL,
            }
        ).api_key
        is None
    )

    with pytest.raises(PrerecordedSTTConfigurationError):
        get_mlx_moss_diarize_config(
            {
                'MLX_MOSS_DIARIZE_ENDPOINT': 'http://speech.example.com/v1/audio/transcriptions',
                'MLX_MOSS_DIARIZE_MODEL': MODEL,
            }
        )
    with pytest.raises(PrerecordedSTTConfigurationError):
        get_mlx_moss_diarize_config(
            {
                'MLX_MOSS_DIARIZE_ENDPOINT': 'http://169.254.169.254/v1/audio/transcriptions',
                'MLX_MOSS_DIARIZE_MODEL': MODEL,
            }
        )
    with pytest.raises(PrerecordedSTTConfigurationError, match='MLX_MOSS_DIARIZE_API_KEY'):
        get_mlx_moss_diarize_config(
            {
                'MLX_MOSS_DIARIZE_ENDPOINT': 'https://speech.example.com/v1/audio/transcriptions',
                'MLX_MOSS_DIARIZE_MODEL': MODEL,
            }
        )
    config = get_mlx_moss_diarize_config(
        {
            'MLX_MOSS_DIARIZE_ENDPOINT': 'https://speech.example.com/v1/audio/transcriptions',
            'MLX_MOSS_DIARIZE_MODEL': MODEL,
            'MLX_MOSS_DIARIZE_API_KEY': 'operator-secret',
        }
    )
    assert config.api_key == 'operator-secret'


def test_official_hosted_moss_authority_is_rejected() -> None:
    for endpoint in (
        'https://api.mosi.cn/v1/audio/transcriptions',
        'https://api.omi.me/v1/audio/transcriptions',
    ):
        with pytest.raises(PrerecordedSTTConfigurationError, match='MLX_MOSS_DIARIZE_ENDPOINT'):
            get_mlx_moss_diarize_config(
                {
                    'MLX_MOSS_DIARIZE_ENDPOINT': endpoint,
                    'MLX_MOSS_DIARIZE_MODEL': MODEL,
                    'MLX_MOSS_DIARIZE_API_KEY': 'hosted-key',
                }
            )


@pytest.mark.parametrize(
    'authority',
    [
        '169.254.169.254',
        '0.0.0.0',
        '224.0.0.1',
        '100.100.100.200',
        '[fe80::1]',
        '[::]',
        '[ff02::1]',
        '[2001:db8::1]',
        '[::ffff:169.254.169.254]',
        'metadata.google.internal',
        'instance-data.ec2.internal',
        '2130706433',
        '0x7f000001',
    ],
)
def test_unsafe_https_endpoint_is_rejected_before_client_construction(monkeypatch, authority) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv(
        'MLX_MOSS_DIARIZE_ENDPOINT',
        f'https://{authority}/v1/audio/transcriptions',
    )
    monkeypatch.setenv('MLX_MOSS_DIARIZE_API_KEY', 'must-not-leak')
    monkeypatch.setattr(mlx_provider, 'get_stt_client', lambda: pytest.fail('unsafe endpoint must stay local'))

    with pytest.raises(PrerecordedSTTConfigurationError, match='MLX_MOSS_DIARIZE_ENDPOINT'):
        MlxMossDiarizePrerecordedProvider().transcribe_bytes(_wav(), encoding='audio/wav')


@pytest.mark.parametrize(
    ('status_code', 'outcome', 'retryable'),
    [
        (401, 'config_error', False),
        (422, 'invalid_input', False),
        (429, 'upstream_error', True),
        (503, 'upstream_error', True),
    ],
)
def test_http_statuses_preserve_retryability_class(monkeypatch, status_code, outcome, retryable) -> None:
    _configure(monkeypatch)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    with pytest.raises(TranscriptionFailure) as raised:
        MlxMossDiarizePrerecordedProvider(client=_client(handler)).transcribe_bytes(_wav(), encoding='audio/wav')

    assert raised.value.outcome.value == outcome
    assert raised.value.retryable is retryable


@pytest.mark.parametrize(
    'keywords',
    [
        ['x' * (mlx_provider.MAX_HOTWORD_CHARACTERS + 1)],
        ['comma,breaks-wire'],
        [f'word-{index}' for index in range(mlx_provider.MAX_HOTWORDS + 1)],
    ],
)
def test_hotwords_are_bounded_before_client_construction(monkeypatch, keywords) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(mlx_provider, 'get_stt_client', lambda: pytest.fail('invalid context must stay local'))

    with pytest.raises(TranscriptionFailure) as raised:
        MlxMossDiarizePrerecordedProvider().transcribe_bytes(_wav(), encoding='audio/wav', keywords=keywords)

    assert raised.value.outcome.value == 'invalid_input'


@pytest.mark.parametrize(
    'audio_url',
    [
        'http://127.0.0.1:9000/private/audio.wav',
        'http://169.254.169.254/latest/meta-data',
        'http://user:password@storage.internal/audio.wav',
        'file:///private/tmp/audio.wav',
    ],
)
def test_url_download_rejects_unconfigured_private_and_unsafe_authorities(monkeypatch, audio_url) -> None:
    _configure(monkeypatch)
    monkeypatch.delenv('MINIO_ENDPOINT', raising=False)
    monkeypatch.delenv('MINIO_PUBLIC_ENDPOINT', raising=False)
    monkeypatch.setattr(mlx_provider, 'get_stt_client', lambda: pytest.fail('unsafe URL must not construct a client'))

    with pytest.raises(TranscriptionFailure) as raised:
        MlxMossDiarizePrerecordedProvider().transcribe_url(audio_url)

    assert raised.value.outcome.value == 'invalid_input'
    assert raised.value.retryable is False


def test_url_download_allows_exact_configured_private_storage_origin(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv('MINIO_PUBLIC_ENDPOINT', 'http://minio:9000')
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == 'GET':
            return httpx.Response(200, content=_wav())
        return httpx.Response(
            200,
            json={'segments': [{'start': 0.0, 'end': 0.5, 'speaker_id': 'S01', 'text': '[S01] local'}]},
        )

    words = MlxMossDiarizePrerecordedProvider(client=_client(handler)).transcribe_url(
        'http://minio:9000/private/audio.wav'
    )

    assert calls == [
        ('GET', 'http://minio:9000/private/audio.wav'),
        ('POST', ENDPOINT),
    ]
    assert words == [{'timestamp': [0.0, 0.5], 'speaker': 'SPEAKER_01', 'text': 'local'}]
