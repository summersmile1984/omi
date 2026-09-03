#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Validate operator-owned production evidence without inventing live results.

The validators bind evidence to the tested source/runtime identities and enforce
shape, but they do not claim to contact a KMS, signing service, or model host.
Those systems remain operator authorities; their references and digests must be
recorded by the real drill before an external cutover can be authorized.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

OBJECT_ID = re.compile(r'^[0-9a-f]{40}$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
IMAGE_DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')
RECOVERY_SCHEMA_VERSION = 3
MODEL_PROVENANCE_SCHEMA_VERSION = 1
CAPABILITY_PROVENANCE_SCHEMA_VERSION = 1

RECOVERY_KEYS = frozenset(
    {
        'schema_version',
        'status',
        'scope',
        'backup_manifest_sha256',
        'source_git_commit',
        'source_git_tree',
        'runtime_config_sha256',
        'restore_host_reference',
        'drill_started_at',
        'drill_completed_at',
        'backup_verified',
        'restore_completed',
        'post_restore_migration_passed',
        'post_restore_auth_smoke_passed',
        'post_restore_projection_checks_passed',
        'isolated_restore_host',
        'key_material_outside_backup',
        'production_kms_attested',
        'key_custody_reference',
        'kms_attestation',
        'signed_artifacts',
    }
)
KMS_ATTESTATION_KEYS = frozenset(
    {'provider', 'key_reference', 'key_version', 'attestation_reference', 'verified_at', 'decrypt_verified'}
)
SIGNED_ARTIFACT_KEYS = frozenset(
    {'artifact', 'digest', 'signature_reference', 'signer_identity', 'verification_method', 'verified_at'}
)
MODEL_PROVENANCE_KEYS = frozenset(
    {
        'schema_version',
        'status',
        'provider',
        'model_id',
        'endpoint_origin',
        'model_sha256',
        'source_reference',
        'service_revision',
        'verification_method',
        'attestation_reference',
        'source_git_commit',
        'source_git_tree',
        'runtime_config_sha256',
    }
)
CAPABILITY_PROVENANCE_KEYS = frozenset(
    {
        'schema_version',
        'status',
        'source_git_commit',
        'source_git_tree',
        'runtime_config_sha256',
        'capabilities',
    }
)
CAPABILITY_NAMES = frozenset(
    {
        'generic_llm',
        'embedding',
        'stt_diarization',
        'realtime',
        'speaker_identity',
        'tts',
    }
)
CAPABILITY_COMMON_KEYS = frozenset(
    {
        'provider',
        'model',
        'endpoint_origin',
        'transport',
        'model_sha256',
        'service_revision',
        'source_reference',
        'verification_method',
        'attestation_reference',
    }
)
CAPABILITY_ROUTE_EXTRA_KEYS = {
    'generic_llm': frozenset(),
    'embedding': frozenset({'dimension'}),
    'stt_diarization': frozenset(),
    'realtime': frozenset({'wire_protocol'}),
    'speaker_identity': frozenset(),
    'tts': frozenset(),
}


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f'{name} must be non-empty text without control characters')
    return value.strip()


def _object_id(value: Any, name: str) -> str:
    result = _text(value, name)
    if OBJECT_ID.fullmatch(result) is None:
        raise ValueError(f'{name} must be a full Git object id')
    return result


def _sha256(value: Any, name: str) -> str:
    result = _text(value, name)
    if SHA256.fullmatch(result) is None:
        raise ValueError(f'{name} must be a SHA-256 digest')
    return result


def _timestamp(value: Any, name: str) -> str:
    result = _text(value, name)
    try:
        datetime.fromisoformat(result.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError(f'{name} must be an ISO-8601 timestamp') from error
    return result


def _origin(value: Any, name: str) -> str:
    result = _text(value, name)
    try:
        parsed = urlsplit(result)
        parsed.port
    except ValueError as error:
        raise ValueError(f'{name} must be a safe endpoint origin') from error
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {'', '/'}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f'{name} must be a safe endpoint origin')
    return f'{parsed.scheme}://{parsed.netloc}'


def _source_binding(payload: Mapping[str, Any], expected_source: Mapping[str, str] | None) -> None:
    source = {
        'source_git_commit': _object_id(payload.get('source_git_commit'), 'source_git_commit'),
        'source_git_tree': _object_id(payload.get('source_git_tree'), 'source_git_tree'),
        'runtime_config_sha256': _sha256(payload.get('runtime_config_sha256'), 'runtime_config_sha256'),
    }
    if expected_source is not None and source != dict(expected_source):
        raise ValueError('operator evidence source/runtime binding does not match tested deployment')


def validate_signed_artifacts(
    value: Any,
    *,
    expected_artifacts: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError('signed_artifacts must be a non-empty list')
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != SIGNED_ARTIFACT_KEYS:
            raise ValueError('signed artifact evidence has an incomplete or unexpected schema')
        artifact = _text(item.get('artifact'), 'signed artifact name')
        if artifact in seen:
            raise ValueError(f'duplicate signed artifact evidence: {artifact}')
        seen.add(artifact)
        digest = _text(item.get('digest'), f'{artifact} digest')
        if IMAGE_DIGEST.fullmatch(digest) is None:
            raise ValueError(f'{artifact} digest must be a content-addressed image digest')
        if expected_artifacts is not None and expected_artifacts.get(artifact) != digest:
            raise ValueError(f'{artifact} signed digest does not match the running workload')
        verification_method = _text(item.get('verification_method'), f'{artifact} verification_method')
        if verification_method not in {'cosign', 'notary-v2', 'operator-signer'}:
            raise ValueError(f'{artifact} uses an unsupported signature verification method')
        normalized.append(
            {
                **item,
                'artifact': artifact,
                'digest': digest,
                'signature_reference': _text(item.get('signature_reference'), f'{artifact} signature_reference'),
                'signer_identity': _text(item.get('signer_identity'), f'{artifact} signer_identity'),
                'verification_method': verification_method,
                'verified_at': _timestamp(item.get('verified_at'), f'{artifact} verified_at'),
            }
        )
    if expected_artifacts is not None and seen != set(expected_artifacts):
        raise ValueError('signed artifact evidence does not cover every required workload')
    return normalized


def validate_recovery_evidence(
    payload: Any,
    *,
    expected_source: Mapping[str, str] | None = None,
    expected_artifacts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != RECOVERY_KEYS:
        raise ValueError('recovery evidence has an incomplete or unexpected schema')
    if payload.get('schema_version') != RECOVERY_SCHEMA_VERSION or payload.get('status') != 'passed':
        raise ValueError('recovery evidence has an unsupported schema or status')
    if payload.get('scope') != 'isolated_restore_host':
        raise ValueError('recovery evidence must identify an isolated restore host')
    _source_binding(payload, expected_source)
    _sha256(payload.get('backup_manifest_sha256'), 'backup_manifest_sha256')
    _text(payload.get('restore_host_reference'), 'restore_host_reference')
    _timestamp(payload.get('drill_started_at'), 'drill_started_at')
    _timestamp(payload.get('drill_completed_at'), 'drill_completed_at')
    for field in (
        'backup_verified',
        'restore_completed',
        'post_restore_migration_passed',
        'post_restore_auth_smoke_passed',
        'post_restore_projection_checks_passed',
        'isolated_restore_host',
        'key_material_outside_backup',
        'production_kms_attested',
    ):
        if payload.get(field) is not True:
            raise ValueError(f'recovery evidence field {field} must be true')
    kms = payload.get('kms_attestation')
    if not isinstance(kms, dict) or set(kms) != KMS_ATTESTATION_KEYS:
        raise ValueError('kms_attestation has an incomplete or unexpected schema')
    _text(kms.get('provider'), 'kms provider')
    _text(kms.get('key_reference'), 'kms key_reference')
    _text(kms.get('key_version'), 'kms key_version')
    _text(kms.get('attestation_reference'), 'kms attestation_reference')
    _timestamp(kms.get('verified_at'), 'kms verified_at')
    if kms.get('decrypt_verified') is not True:
        raise ValueError('kms decrypt_verified must be true')
    _text(payload.get('key_custody_reference'), 'key_custody_reference')
    signed = validate_signed_artifacts(payload.get('signed_artifacts'), expected_artifacts=expected_artifacts)
    return {**payload, 'kms_attestation': dict(kms), 'signed_artifacts': signed}


def validate_model_provenance(
    payload: Any,
    *,
    expected_source: Mapping[str, str] | None = None,
    expected_model: str | None = None,
    expected_endpoint_origin: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != MODEL_PROVENANCE_KEYS:
        raise ValueError('model provenance evidence has an incomplete or unexpected schema')
    if payload.get('schema_version') != MODEL_PROVENANCE_SCHEMA_VERSION or payload.get('status') != 'passed':
        raise ValueError('model provenance evidence has an unsupported schema or status')
    if payload.get('provider') != 'mlx_moss_diarize':
        raise ValueError('model provenance evidence must identify mlx_moss_diarize')
    model_id = _text(payload.get('model_id'), 'model_id')
    if expected_model is not None and model_id != expected_model:
        raise ValueError('model provenance model_id does not match the live configured model')
    endpoint_origin = _origin(payload.get('endpoint_origin'), 'endpoint_origin')
    if expected_endpoint_origin is not None and endpoint_origin != _origin(
        expected_endpoint_origin, 'expected endpoint'
    ):
        raise ValueError('model provenance endpoint does not match the live configured endpoint')
    _sha256(payload.get('model_sha256'), 'model_sha256')
    _text(payload.get('source_reference'), 'source_reference')
    _text(payload.get('service_revision'), 'service_revision')
    method = _text(payload.get('verification_method'), 'verification_method')
    if method not in {'sha256-manifest', 'signed-release', 'operator-attestation'}:
        raise ValueError('model provenance uses an unsupported verification method')
    _text(payload.get('attestation_reference'), 'attestation_reference')
    _source_binding(payload, expected_source)
    return {**payload, 'model_id': model_id, 'endpoint_origin': endpoint_origin, 'verification_method': method}


def _capability_endpoint_origin(value: Any, name: str, *, schemes: set[str]) -> str:
    """Normalize a capability origin while allowing local model routes."""

    if value == '':
        return ''
    result = _text(value, name)
    try:
        parsed = urlsplit(result)
        parsed.port
    except ValueError as error:
        raise ValueError(f'{name} must be a safe endpoint origin') from error
    if (
        parsed.scheme not in schemes
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {'', '/'}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f'{name} must be a safe endpoint origin')
    return f'{parsed.scheme}://{parsed.netloc}'


def _capability_route(
    value: Any,
    name: str,
    *,
    expected: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f'{name} capability evidence must be an object')
    expected_keys = CAPABILITY_COMMON_KEYS | CAPABILITY_ROUTE_EXTRA_KEYS[name]
    if set(value) != expected_keys:
        raise ValueError(f'{name} capability evidence has an incomplete or unexpected schema')
    provider = _text(value.get('provider'), f'{name}.provider')
    model = _text(value.get('model'), f'{name}.model')
    transport = _text(value.get('transport'), f'{name}.transport')
    endpoint_origin = value.get('endpoint_origin')
    if name == 'realtime':
        endpoint_origin = _capability_endpoint_origin(endpoint_origin, f'{name}.endpoint_origin', schemes={'ws', 'wss'})
    else:
        endpoint_origin = _capability_endpoint_origin(
            endpoint_origin, f'{name}.endpoint_origin', schemes={'http', 'https'}
        )
    model_sha256 = _sha256(value.get('model_sha256'), f'{name}.model_sha256')
    service_revision = _text(value.get('service_revision'), f'{name}.service_revision')
    source_reference = _text(value.get('source_reference'), f'{name}.source_reference')
    verification_method = _text(value.get('verification_method'), f'{name}.verification_method')
    if verification_method not in {'sha256-manifest', 'signed-release', 'operator-attestation'}:
        raise ValueError(f'{name} uses an unsupported verification method')
    attestation_reference = _text(value.get('attestation_reference'), f'{name}.attestation_reference')
    normalized: dict[str, Any] = {
        'provider': provider,
        'model': model,
        'endpoint_origin': endpoint_origin,
        'transport': transport,
        'model_sha256': model_sha256,
        'service_revision': service_revision,
        'source_reference': source_reference,
        'verification_method': verification_method,
        'attestation_reference': attestation_reference,
    }
    if name == 'embedding':
        dimension = value.get('dimension')
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise ValueError('embedding.dimension must be a positive integer')
        normalized['dimension'] = dimension
    elif name == 'realtime':
        normalized['wire_protocol'] = _text(value.get('wire_protocol'), 'realtime.wire_protocol')
        if normalized['wire_protocol'] != 'openai_realtime_v1':
            raise ValueError('realtime.wire_protocol must be openai_realtime_v1')
    if expected is not None:
        identity_keys = ('provider', 'model', 'endpoint_origin', 'transport')
        for key in identity_keys:
            if normalized[key] != expected.get(key):
                raise ValueError(f'{name} capability identity does not match the running provider route')
        if name == 'embedding' and normalized['dimension'] != expected.get('dimension'):
            raise ValueError('embedding capability dimension does not match the running provider route')
        if name == 'realtime' and normalized['wire_protocol'] != expected.get('wire_protocol'):
            raise ValueError('realtime capability wire protocol does not match the running provider route')
    return normalized


def validate_capability_provenance(
    payload: Any,
    *,
    expected_source: Mapping[str, str] | None = None,
    expected_routes: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate operator provenance for every model-backed capability.

    A green transport probe alone cannot prove that the relay, MOSS service,
    embedding backend, speaker model, or TTS artifact is the reviewed route.
    This record carries non-secret model/service identities and is deliberately
    an operator evidence contract: the repository validates its bindings but
    never pretends to contact a registry, signer, or KMS.
    """

    if not isinstance(payload, dict) or set(payload) != CAPABILITY_PROVENANCE_KEYS:
        raise ValueError('capability provenance evidence has an incomplete or unexpected schema')
    if payload.get('schema_version') != CAPABILITY_PROVENANCE_SCHEMA_VERSION or payload.get('status') != 'passed':
        raise ValueError('capability provenance evidence has an unsupported schema or status')
    _source_binding(payload, expected_source)
    capabilities = payload.get('capabilities')
    if not isinstance(capabilities, dict) or set(capabilities) != CAPABILITY_NAMES:
        raise ValueError('capability provenance evidence must cover every model-backed capability')
    normalized_capabilities: dict[str, Any] = {}
    for name in sorted(CAPABILITY_NAMES):
        expected = expected_routes.get(name) if expected_routes is not None else None
        normalized_capabilities[name] = _capability_route(capabilities[name], name, expected=expected)
    return {**payload, 'capabilities': normalized_capabilities}


def _load(path: Path) -> Any:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('operator evidence path must be an existing absolute regular non-symlink file')
    return json.loads(path.read_text(encoding='utf-8'))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    for name in ('recovery', 'model', 'capabilities'):
        command = subparsers.add_parser(name)
        command.add_argument('path', type=Path)
    arguments = parser.parse_args(argv)
    try:
        value = _load(arguments.path)
        if arguments.command == 'recovery':
            normalized = validate_recovery_evidence(value)
        elif arguments.command == 'model':
            normalized = validate_model_provenance(value)
        else:
            normalized = validate_capability_provenance(value)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1
    print(json.dumps(normalized, sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
