from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / 'deploy' / 'self-host' / 'runtime_provider_attestation.py'
SPEC = importlib.util.spec_from_file_location('self_host_runtime_provider_attestation', MODULE_PATH)
assert SPEC and SPEC.loader
attestation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(attestation)


def _configuration() -> dict[str, object]:
    return {
        'stt_prerecorded_model': 'mlx_moss_diarize',
        'mlx_moss_diarize_endpoint': 'http://moss:8000/v1/audio/transcriptions',
        'mlx_moss_diarize_model': 'moss-zh-en',
        'generic_llm_provider': 'generic',
        'generic_llm_model': 'operator-chat',
        'generic_llm_transport': 'openai_compatible_http',
        'generic_llm_endpoint_origin': 'http://llm:8000',
        'embedding_provider': 'generic',
        'embedding_model': 'operator-embedding',
        'embedding_transport': 'direct',
        'embedding_dimension': '512',
        'speaker_embedding_provider': 'sherpa_onnx',
        'speaker_embedding_model': 'speaker.onnx',
        'realtime_provider': 'relay',
        'realtime_model': 'operator-realtime',
        'realtime_transport': 'websocket_relay',
        'realtime_endpoint_origin': 'ws://relay:8765',
        'realtime_wire_protocol': 'openai_realtime_v1',
        'tts_provider': 'sherpa_onnx',
        'tts_model': 'tts.onnx',
        'tts_transport': 'local',
        'tts_endpoint_origin': '',
        'file_chat_provider': 'local_extraction',
        'file_chat_model': 'operator-chat',
        'file_chat_transport': 'local_extraction',
        'app_icon_transport': 'local_template',
        'app_icon_endpoint_origin': '',
        'push_provider': 'disabled',
        'push_model': 'disabled',
        'push_transport': 'disabled',
        'push_endpoint_origin': '',
    }


SOURCE = {
    'image_id': 'sha256:' + '1' * 64,
    'git_commit': '2' * 40,
    'git_tree': '3' * 40,
    'runtime_config_sha256': '4' * 64,
}


def test_attestation_contains_explicit_capability_scopes_without_provenance_fiction() -> None:
    config = _configuration()
    result = attestation.build_provider_attestation(
        expected_configuration=config,
        runtime_configuration=config,
        source=SOURCE,
    )

    routes = result['capability_routes']
    assert routes['realtime']['roundtrip_scope'] == 'transport_only'
    assert routes['realtime']['model_provenance_attested'] is False
    assert routes['stt_diarization']['service_revision_attested'] is False
    assert routes['stt_diarization']['model_revision_attested'] is False
    assert routes['speaker_identity']['identity_scope'] == 'embedding_only'
    assert routes['speaker_identity']['enrollment_match_attested'] is False
    assert routes['push']['receipt_schema'] == 'omi.push.receipt.v1'
    assert routes['push']['delivery_scope'] == 'receipt_required_per_device'

    attestation.validate_provider_attestation(result, expected_configuration=config, expected_source=SOURCE)


def test_attestation_rejects_overstated_realtime_or_speaker_evidence() -> None:
    config = _configuration()
    result = attestation.build_provider_attestation(
        expected_configuration=config,
        runtime_configuration=config,
        source=SOURCE,
    )
    result['capability_routes']['realtime']['model_provenance_attested'] = True
    with pytest.raises(ValueError, match='overstated provenance'):
        attestation.validate_provider_attestation(result)


def test_realtime_probe_requires_transport_scope_and_no_model_claim() -> None:
    config = _configuration()
    probe = {
        'status': 'passed',
        'provider': 'relay',
        'model': 'operator-realtime',
        'endpoint_origin': 'ws://relay:8765',
        'transport': 'websocket_relay',
        'wire_protocol': 'openai_realtime_v1',
        'roundtrip_scope': 'transport_only',
        'model_provenance_attested': False,
    }
    attestation.validate_realtime_probe_identity(probe, config)
    probe['roundtrip_scope'] = 'model_inference'
    with pytest.raises(ValueError, match='route identity'):
        attestation.validate_realtime_probe_identity(probe, config)
