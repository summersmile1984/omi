#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Build the self-host acceptance change record without overstating evidence."""

from __future__ import annotations

import argparse
import json
import os
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
    operator_policy_hash = live_egress.get('operator_policy_artifact_sha256') if isinstance(live_egress, dict) else None
    sentinel_policy_verified = bool(
        isinstance(live_egress, dict)
        and live_egress.get('enforcement') == 'sentinel_targets_denied_with_operator_policy'
        and expected_sentinels.issubset(set(live_egress.get('sentinel_targets_denied') or []))
        and {'backend', 'queue-worker', 'auth-server'}.issubset(set(live_egress.get('workloads') or []))
        and isinstance(operator_policy_hash, str)
        and len(operator_policy_hash) == 64
    )
    long_term_admission_passed = bool(
        isinstance(assembled_loop, dict)
        and assembled_loop.get('assembled_product_loop', {}).get('remember', {}).get('long_term_admission') == 'passed'
    )
    tested_configuration_authorized = bool(
        mode in {'cutover-live', 'external-cutover-live'}
        and isinstance(assembled_loop, dict)
        and assembled_loop.get('status') == 'passed'
        and isinstance(live_replacement, dict)
        and live_replacement.get('status') == 'passed'
        and long_term_admission_passed
        and worktree_clean
    )
    production_authorized = bool(external_cutover and tested_configuration_authorized and sentinel_policy_verified)
    return {
        'schema_version': 2,
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
            'production_services_healthy': 'passed' if mode != 'contracts' else 'not_run',
            'live_replacement_services': live_replacement or 'not_run',
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
                                'intended_public_dns_certificate_and_edge_not_exercised'
                                if not external_cutover
                                else 'live_sentinel_egress_or_operator_policy_evidence_missing'
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
    evidence = build_evidence(
        mode=mode,
        source_attribution=json.loads(os.environ['SOURCE_ATTRIBUTION_JSON']),
        live_replacement=live_replacement,
        assembled_loop=assembled_loop,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
    path = Path(os.environ['ACCEPTANCE_EVIDENCE'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f'zero-vendor acceptance evidence: {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
