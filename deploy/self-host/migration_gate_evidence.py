#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Write the private, source-bound evidence emitted by the migration gate.

The migration gate is deliberately not a traffic switch.  This helper keeps
its change record tied to the exact source tree and isolated test runtime, and
writes it atomically as a mode-0600 regular file.  It contains no customer
data and never signs or authorizes a production route.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

OBJECT_ID = re.compile(r'^[0-9a-f]{40}$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
SCHEMA_VERSION = 2


def _required(value: str | None, name: str) -> str:
    result = str(value or '').strip()
    if not result:
        raise ValueError(f'{name} is required')
    return result


def _object_id(value: str | None, name: str) -> str:
    result = _required(value, name)
    if not OBJECT_ID.fullmatch(result):
        raise ValueError(f'{name} must be a full Git object id')
    return result


def _sha256(value: str | None, name: str) -> str:
    result = _required(value, name)
    if not SHA256.fullmatch(result):
        raise ValueError(f'{name} must be a SHA-256 digest')
    return result


def build_evidence(
    *,
    git_sha: str,
    git_tree: str,
    compose_file: Path,
    project: str,
    mode: str,
    ports: Mapping[str, str],
    postgres_target: str,
    firestore_emulator: str,
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic-schema migration gate record.

    ``runtime_config_sha256`` fingerprints the exact disposable project,
    ports, and reviewed Compose bytes used by the gate.  It is intentionally
    distinct from the production Compose runtime fingerprint consumed by the
    zero-vendor acceptance record.
    """

    source_git_sha = _object_id(git_sha, 'git_sha')
    source_git_tree = _object_id(git_tree, 'git_tree')
    compose = compose_file.resolve()
    if compose.is_symlink() or not compose.is_file():
        raise ValueError('compose file must be a regular file')
    compose_sha256 = hashlib.sha256(compose.read_bytes()).hexdigest()
    required_ports = {
        'postgres_port',
        'firestore_emulator_port',
        'firebase_auth_emulator_port',
        'firebase_storage_emulator_port',
        'better_auth_port',
    }
    if set(ports) != required_ports:
        raise ValueError('migration runtime ports are incomplete')
    runtime_identity = {
        'project': _required(project, 'project'),
        'mode': 'managed-isolated' if mode == '1' else 'external-disposable' if mode == '0' else mode,
        **{key: _required(ports[key], key) for key in sorted(required_ports)},
        'compose_sha256': compose_sha256,
    }
    runtime_config_sha256 = hashlib.sha256(
        json.dumps(runtime_identity, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    return {
        'schema_version': SCHEMA_VERSION,
        'status': 'GO',
        'checked_at': checked_at or datetime.now(timezone.utc).isoformat(),
        'git_sha': source_git_sha,
        'git_tree': source_git_tree,
        'runtime_config_sha256': runtime_config_sha256,
        'runtime_identity': runtime_identity,
        'target': {
            'postgres': _required(postgres_target, 'postgres target'),
            'firestore_emulator': _required(firestore_emulator, 'firestore emulator'),
        },
        'gates': {
            'zero_vendor_static_config': 'passed',
            'better_auth_schema_migration': 'passed',
            'better_auth_session_jwt_jwks_backend_verification': 'passed',
            'firestore_pg_versioned_schema_migration': 'passed',
            'firestore_to_pg_full_path_import_reconciliation': 'passed',
            'firestore_pg_live_integration': 'passed',
            'firestore_emulator_shadow_diff': 'passed',
        },
        'authorizes_traffic_change': False,
    }


def write_evidence(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write a mode-0600 evidence object without following links."""

    if not path.is_absolute() or path == Path('/'):
        raise ValueError('evidence path must be an absolute non-root path')
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('migration evidence path is not a regular file')
    if path.exists() and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError('existing migration evidence must be mode 0600')
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(dict(payload), indent=2, sort_keys=True) + '\n').encode('utf-8')
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='wb', dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp', delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, 0o600)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    ports = {
        'postgres_port': os.getenv('GATE_PG_PORT'),
        'firestore_emulator_port': os.getenv('GATE_FIRESTORE_PORT'),
        'firebase_auth_emulator_port': os.getenv('GATE_AUTH_PORT'),
        'firebase_storage_emulator_port': os.getenv('GATE_STORAGE_PORT'),
        'better_auth_port': os.getenv('GATE_BETTER_AUTH_PORT'),
    }
    payload = build_evidence(
        git_sha=os.getenv('GATE_GIT_SHA', ''),
        git_tree=os.getenv('GATE_GIT_TREE', ''),
        compose_file=Path(_required(os.getenv('GATE_COMPOSE_FILE'), 'GATE_COMPOSE_FILE')),
        project=os.getenv('GATE_PROJECT', ''),
        mode=os.getenv('GATE_MODE', ''),
        ports=ports,
        postgres_target=os.getenv('GATE_PG_DSN_HOST', ''),
        firestore_emulator=os.getenv('GATE_EMULATOR_HOST', ''),
    )
    path = Path(_required(os.getenv('GATE_EVIDENCE_FILE'), 'GATE_EVIDENCE_FILE'))
    write_evidence(path, payload)
    print(f'migration cutover evidence: {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
