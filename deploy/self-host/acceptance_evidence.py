#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Build the self-host acceptance change record without overstating evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _git(root: Path, *args: str, environment: dict[str, str] | None = None) -> str:
    git_environment = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', **(environment or {})}
    result = subprocess.run(
        ['git', '-C', str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        env=git_environment,
    )
    if result.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def resolve_source_attribution(root: Path, *, require_clean: bool) -> dict[str, Any]:
    """Resolve the exact tested tree without mutating the operator's index."""

    repository = Path(_git(root, 'rev-parse', '--show-toplevel')).resolve()
    git_commit = _git(repository, 'rev-parse', 'HEAD')
    dirty_lines = _git(repository, 'status', '--porcelain=v1', '--untracked-files=all').splitlines()
    worktree_clean = not dirty_lines
    if require_clean and not worktree_clean:
        raise RuntimeError(
            'cutover acceptance requires a clean worktree; commit or remove every tracked/untracked change first'
        )
    if worktree_clean:
        git_tree = _git(repository, 'rev-parse', 'HEAD^{tree}')
    else:
        with tempfile.TemporaryDirectory(prefix='omi-acceptance-index-') as directory:
            environment = {**os.environ, 'GIT_INDEX_FILE': str(Path(directory) / 'index')}
            _git(repository, 'read-tree', 'HEAD', environment=environment)
            _git(repository, 'add', '-A', '--', '.', environment=environment)
            git_tree = _git(repository, 'write-tree', environment=environment)
    return {
        'git_commit': git_commit,
        'git_tree': git_tree,
        'worktree_clean': worktree_clean,
    }


def build_evidence(
    *,
    mode: str,
    source_attribution: dict[str, Any],
    live_replacement: dict[str, Any] | None,
    assembled_loop: dict[str, Any] | None,
    checked_at: str,
    runtime_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    git_commit = source_attribution.get('git_commit')
    git_tree = source_attribution.get('git_tree')
    worktree_clean = source_attribution.get('worktree_clean') is True
    if not isinstance(git_commit, str) or len(git_commit) != 40:
        raise ValueError('source attribution git_commit must be a full Git object id')
    if not isinstance(git_tree, str) or len(git_tree) != 40:
        raise ValueError('source attribution git_tree must be a full Git object id')
    external_cutover = mode == 'external-cutover-live'
    live_egress = assembled_loop.get('live_egress', {}) if isinstance(assembled_loop, dict) else 'not_run'
    expected_sentinels = {
        'api.openai.com',
        'generativelanguage.googleapis.com',
        'api.anthropic.com',
        'api.omi.me',
        '1.1.1.1',
    }
    expected_policy_workloads = ['auth-server', 'backend', 'queue-worker']
    expected_policy_targets = [
        '1.1.1.1',
        'api.openai.com',
        'api.omi.me',
        'api.anthropic.com',
        'generativelanguage.googleapis.com',
    ]
    operator_policy_hash = live_egress.get('operator_policy_artifact_sha256') if isinstance(live_egress, dict) else None
    sentinel_policy_verified = bool(
        isinstance(live_egress, dict)
        and live_egress.get('enforcement') == 'sentinel_targets_denied_with_operator_policy'
        and expected_sentinels.issubset(set(live_egress.get('sentinel_targets_denied') or []))
        and {'backend', 'queue-worker', 'auth-server'}.issubset(set(live_egress.get('workloads') or []))
        and live_egress.get('operator_policy_schema_version') == 1
        and live_egress.get('operator_policy_workloads') == expected_policy_workloads
        and live_egress.get('operator_policy_denied_targets') == expected_policy_targets
        and isinstance(operator_policy_hash, str)
        and len(operator_policy_hash) == 64
    )
    long_term_admission_passed = bool(
        isinstance(assembled_loop, dict)
        and assembled_loop.get('assembled_product_loop', {}).get('remember', {}).get('long_term_admission') == 'passed'
    )
    public_object_signed_crud_passed = bool(
        isinstance(assembled_loop, dict)
        and assembled_loop.get('https_origin_and_hairpin', {}).get('public_object_signed_crud', {}).get('status')
        == 'passed'
    )
    speaker_embedding_passed = bool(
        isinstance(assembled_loop, dict)
        and assembled_loop.get('assembled_product_loop', {})
        .get('capture', {})
        .get('speaker_embedding', {})
        .get('status')
        == 'passed'
    )
    speaker_diarization = (
        assembled_loop.get('assembled_product_loop', {}).get('capture', {}).get('speaker_diarization', {})
        if isinstance(assembled_loop, dict)
        else {}
    )
    diarization_route = speaker_diarization.get('route', {}) if isinstance(speaker_diarization, dict) else {}
    diarization_model = speaker_diarization.get('configured_model') if isinstance(speaker_diarization, dict) else None
    diarization_audio_sha256 = (
        speaker_diarization.get('audio_sha256') if isinstance(speaker_diarization, dict) else None
    )
    diarization_duration = (
        speaker_diarization.get('audio_duration_seconds') if isinstance(speaker_diarization, dict) else None
    )
    runtime_identity = runtime_evidence.get('runtime_identity', {}) if isinstance(runtime_evidence, dict) else {}
    effective_provider_configuration = (
        runtime_identity.get('effective_provider_configuration', {}) if isinstance(runtime_identity, dict) else {}
    )
    speaker_diarization_passed = bool(
        isinstance(speaker_diarization, dict)
        and speaker_diarization.get('status') == 'passed'
        and speaker_diarization.get('provider') == 'mlx_moss_diarize'
        and isinstance(diarization_route, dict)
        and re.fullmatch(r'https?://[^/]+', str(diarization_route.get('endpoint_origin') or '')) is not None
        and diarization_route.get('transcription_path') == '/v1/audio/transcriptions'
        and diarization_route.get('models_catalog_path') == '/v1/models'
        and diarization_route.get('multipart_model') == diarization_model
        and diarization_route.get('response_format') == 'verbose_json'
        and diarization_route.get('authorization') in {'none', 'bearer'}
        and isinstance(diarization_model, str)
        and bool(diarization_model)
        and speaker_diarization.get('model_catalog_exact_id_match') is True
        and speaker_diarization.get('real_transcription_route_exercised') is True
        and isinstance(diarization_audio_sha256, str)
        and re.fullmatch(r'[0-9a-f]{64}', diarization_audio_sha256) is not None
        and isinstance(diarization_duration, (int, float))
        and 0 < float(diarization_duration) <= 6 * 60 * 60
        and int(speaker_diarization.get('segment_count') or 0) > 0
        and int(speaker_diarization.get('speaker_count') or 0) >= 2
        and int(speaker_diarization.get('speaker_transition_count') or 0) >= 2
        and speaker_diarization.get('audio_duration_source') == 'wav_header_frames_divided_by_sample_rate'
        and speaker_diarization.get('service_revision_reported') is False
        and speaker_diarization.get('operator_model_source_attested_by_gate') is False
    )
    diarization_runtime_config_binding_passed = bool(
        speaker_diarization_passed
        and isinstance(effective_provider_configuration, dict)
        and effective_provider_configuration.get('stt_prerecorded_model') == 'mlx_moss_diarize'
        and effective_provider_configuration.get('mlx_moss_diarize_model') == diarization_model
        and effective_provider_configuration.get('mlx_moss_diarize_endpoint')
        == f'{diarization_route.get("endpoint_origin", "")}{diarization_route.get("transcription_path", "")}'
    )
    model_artifact_identity = (
        assembled_loop.get('assembled_product_loop', {}).get('capture', {}).get('mounted_model_artifact_identity', {})
        if isinstance(assembled_loop, dict)
        else {}
    )
    required_model_artifacts = {
        'sensevoice_model',
        'sensevoice_tokens',
        'speaker_embedding_model',
        'tts_model',
        'tts_tokens',
    }
    model_artifacts = model_artifact_identity.get('artifacts', {}) if isinstance(model_artifact_identity, dict) else {}
    model_artifact_identity_passed = bool(
        isinstance(model_artifact_identity, dict)
        and model_artifact_identity.get('status') == 'passed'
        and model_artifact_identity.get('paths_recorded') is False
        and isinstance(model_artifacts, dict)
        and set(model_artifacts) == required_model_artifacts
        and all(
            isinstance(value, dict)
            and isinstance(value.get('sha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', value['sha256']) is not None
            and int(value.get('bytes') or 0) > 0
            for value in model_artifacts.values()
        )
    )
    assembled_product_loop = (
        assembled_loop.get('assembled_product_loop', {}) if isinstance(assembled_loop, dict) else {}
    )
    hard_capability_status = {
        name: bool(
            isinstance(assembled_product_loop.get(name), dict)
            and assembled_product_loop[name].get('status') == 'passed'
        )
        for name in (
            'realtime_relay',
            'tts',
            'app_icon',
            'file_chat',
            'typesense_keyword',
            'conversation_typesense',
            'firmware',
        )
    }
    failed_hard_capability = next((name for name, passed in hard_capability_status.items() if not passed), None)
    runtime_health_and_identity_passed = bool(
        isinstance(runtime_evidence, dict)
        and runtime_evidence.get('status') == 'passed'
        and runtime_evidence.get('all_required_services_healthy') is True
        and runtime_identity.get('source_and_config_match') is True
        and runtime_identity.get('expected_git_commit') == git_commit
        and runtime_identity.get('expected_git_tree') == git_tree
        and isinstance(runtime_identity.get('expected_config_sha256'), str)
        and len(runtime_identity.get('expected_config_sha256')) == 64
    )
    tested_configuration_authorized = bool(
        mode in {'cutover-live', 'external-cutover-live'}
        and isinstance(assembled_loop, dict)
        and assembled_loop.get('status') == 'passed'
        and isinstance(live_replacement, dict)
        and live_replacement.get('status') == 'passed'
        and long_term_admission_passed
        and speaker_embedding_passed
        and speaker_diarization_passed
        and diarization_runtime_config_binding_passed
        and model_artifact_identity_passed
        and public_object_signed_crud_passed
        and all(hard_capability_status.values())
        and runtime_health_and_identity_passed
        and worktree_clean
    )
    production_authorized = bool(external_cutover and tested_configuration_authorized and sentinel_policy_verified)
    return {
        'schema_version': 3,
        'checked_at': checked_at,
        'git_commit': git_commit,
        'git_tree': git_tree,
        'worktree_clean': worktree_clean,
        'mode': mode,
        'gates': {
            'zero_vendor_static_config': 'passed',
            'hermetic_undeclared_dns_and_socket_egress': 'denied',
            'live_sentinel_egress_policy': live_egress,
            'live_dns_denial_claimed': False,
            'hermetic_capture_understand_remember_retrieve_act_contract': 'passed',
            'hermetic_account_deletion_contract': 'passed',
            'hermetic_contract_uses_replacement_services': False,
            'live_capture_understand_remember_retrieve_act': assembled_loop or 'not_run',
            'production_services_healthy': runtime_evidence or 'not_run',
            'runtime_source_and_config_identity': runtime_identity or 'not_run',
            'live_replacement_services': live_replacement or 'not_run',
            'live_hard_capability_probes': hard_capability_status,
            'live_mlx_moss_diarization_provider': speaker_diarization or 'not_run',
            'live_mlx_moss_runtime_config_binding': diarization_runtime_config_binding_passed,
            'mounted_model_artifact_identity': model_artifact_identity or 'not_run',
        },
        'authorizes_tested_configuration_cutover': tested_configuration_authorized,
        'authorizes_production_cutover': production_authorized,
        'remaining_cutover_reason': (
            None
            if production_authorized
            else (
                'source_worktree_not_clean'
                if not worktree_clean
                else (
                    'assembled_live_product_loop_not_passed'
                    if not isinstance(assembled_loop, dict) or assembled_loop.get('status') != 'passed'
                    else (
                        'live_replacement_services_not_passed'
                        if not isinstance(live_replacement, dict) or live_replacement.get('status') != 'passed'
                        else (
                            'canonical_long_term_admission_not_passed'
                            if not long_term_admission_passed
                            else (
                                'speaker_embedding_not_passed'
                                if not speaker_embedding_passed
                                else (
                                    'speaker_diarization_not_passed'
                                    if not speaker_diarization_passed
                                    else (
                                        'speaker_diarization_runtime_config_binding_not_passed'
                                        if not diarization_runtime_config_binding_passed
                                        else (
                                            'mounted_model_artifact_identity_not_passed'
                                            if not model_artifact_identity_passed
                                            else (
                                                'public_object_signed_crud_not_passed'
                                                if not public_object_signed_crud_passed
                                                else (
                                                    f'{failed_hard_capability}_not_passed'
                                                    if failed_hard_capability is not None
                                                    else (
                                                        'production_service_health_or_runtime_identity_not_passed'
                                                        if not runtime_health_and_identity_passed
                                                        else (
                                                            'intended_public_dns_certificate_and_edge_not_exercised'
                                                            if not external_cutover
                                                            else 'live_sentinel_egress_or_operator_policy_evidence_missing'
                                                        )
                                                    )
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-attribution', action='store_true')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--require-clean', action='store_true')
    arguments = parser.parse_args()
    if arguments.source_attribution:
        try:
            attribution = resolve_source_attribution(arguments.root, require_clean=arguments.require_clean)
        except RuntimeError as error:
            print(f'ERROR: {error}', file=sys.stderr)
            return 1
        print(json.dumps(attribution, sort_keys=True))
        return 0

    mode = os.environ['ACCEPTANCE_MODE']
    live_replacement = json.loads(os.environ['LIVE_REPLACEMENT_JSON']) if mode != 'contracts' else None
    assembled_loop = (
        json.loads(os.environ['ASSEMBLED_LOOP_JSON']) if mode in {'cutover-live', 'external-cutover-live'} else None
    )
    runtime_evidence = json.loads(os.environ['RUNTIME_EVIDENCE_JSON']) if mode != 'contracts' else None
    evidence = build_evidence(
        mode=mode,
        source_attribution=json.loads(os.environ['SOURCE_ATTRIBUTION_JSON']),
        live_replacement=live_replacement,
        assembled_loop=assembled_loop,
        checked_at=datetime.now(timezone.utc).isoformat(),
        runtime_evidence=runtime_evidence,
    )
    path = Path(os.environ['ACCEPTANCE_EVIDENCE'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f'zero-vendor acceptance evidence: {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
