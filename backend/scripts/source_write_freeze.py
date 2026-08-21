#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Issue and verify a short-lived source-write freeze lease.

The Firestore and object-storage migrations read a live source more than once.
An operator must therefore provide one explicit, time-bounded lease proving
that source writes are paused for the complete reconciliation window.  The
lease is an HMAC-signed, mode-0600 JSON artifact; the secret is supplied only
through ``OMI_SOURCE_WRITE_FREEZE_SECRET`` and is never written to disk.

This is a narrow coordination contract, not a provider API.  It deliberately
does not pretend to pause a source by itself: the operator (or an external
change-control system) issues the lease after pausing writes, and every
destructive/cutover CLI verifies it again immediately before reading or
promoting data.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
FORMAT = 'omi-source-write-freeze-v1'
SECRET_ENV = 'OMI_SOURCE_WRITE_FREEZE_SECRET'
SCOPES = frozenset({'firestore', 'storage'})
MAX_TTL_SECONDS = 24 * 60 * 60
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_LEASE_ID = re.compile(r'^[0-9a-f-]{36}$')
_REQUIRED_KEYS = frozenset(
    {
        'schema_version',
        'format',
        'lease_id',
        'status',
        'holder',
        'source',
        'scopes',
        'issued_at',
        'expires_at',
        'signature',
    }
)
_SOURCE_KEYS = frozenset({'project', 'database', 'endpoint'})


class SourceWriteFreezeError(RuntimeError):
    """The source-write freeze artifact is absent, invalid, or expired."""


def canonical_endpoint(value: str) -> str:
    """Return a credential-free, path-free source endpoint identity."""

    raw = str(value or '').strip().rstrip('/')
    if not raw:
        raise SourceWriteFreezeError('source endpoint must be explicit')
    parsed = urlsplit(raw if '://' in raw else f'//{raw}')
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise SourceWriteFreezeError('source endpoint must not contain credentials, query, or fragment')
    host = (parsed.hostname or '').lower().rstrip('.')
    if not host:
        raise SourceWriteFreezeError('source endpoint host is missing')
    try:
        port = parsed.port
    except ValueError as error:
        raise SourceWriteFreezeError('source endpoint port is invalid') from error
    if parsed.path not in {'', '/'}:
        raise SourceWriteFreezeError('source endpoint must not contain a path')
    if parsed.scheme:
        if parsed.scheme not in {'http', 'https'}:
            raise SourceWriteFreezeError('source endpoint scheme must be http or https')
    rendered_host = f'[{host}]' if ':' in host else host
    return f'{rendered_host}:{port}' if port is not None else rendered_host


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _signature(payload: Mapping[str, Any], secret: str) -> str:
    if not secret:
        raise SourceWriteFreezeError(f'{SECRET_ENV} must be set')
    return hmac.new(secret.encode('utf-8'), _canonical_json(payload), hashlib.sha256).hexdigest()


def _parse_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise SourceWriteFreezeError(f'lease {field} must be an ISO-8601 timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise SourceWriteFreezeError(f'lease {field} is invalid') from error
    if parsed.tzinfo is None:
        raise SourceWriteFreezeError(f'lease {field} must include a timezone')
    return parsed.astimezone(timezone.utc)


def _read_lease(path: Path) -> dict[str, Any]:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise SourceWriteFreezeError(f'freeze lease is not readable: {path}') from error
    if mode & 0o077:
        raise SourceWriteFreezeError('freeze lease must be mode 0600 or stricter')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SourceWriteFreezeError('freeze lease is not valid UTF-8 JSON') from error
    if not isinstance(value, dict) or set(value) != _REQUIRED_KEYS:
        raise SourceWriteFreezeError('freeze lease has an unsupported schema or extra fields')
    return value


def verify_lease(
    path: Path,
    *,
    source_project: str,
    source_database: str,
    source_endpoint: str,
    required_scopes: set[str] | frozenset[str] | tuple[str, ...],
    secret: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify a lease against the exact source authority and requested scopes."""

    expected_scopes = set(required_scopes)
    if not expected_scopes or not expected_scopes <= SCOPES:
        raise SourceWriteFreezeError('required freeze scopes must be firestore and/or storage')
    lease = _read_lease(path)
    if lease['schema_version'] != SCHEMA_VERSION or lease['format'] != FORMAT or lease['status'] != 'active':
        raise SourceWriteFreezeError('freeze lease is not an active supported lease')
    lease_id = lease['lease_id']
    if not isinstance(lease_id, str) or not _LEASE_ID.fullmatch(lease_id):
        raise SourceWriteFreezeError('freeze lease id is invalid')
    holder = lease['holder']
    if not isinstance(holder, str) or not holder.strip() or len(holder) > 200:
        raise SourceWriteFreezeError('freeze lease holder is invalid')
    source = lease['source']
    if not isinstance(source, dict) or set(source) != _SOURCE_KEYS:
        raise SourceWriteFreezeError('freeze lease source authority is invalid')
    expected_source = {
        'project': str(source_project).strip(),
        'database': str(source_database).strip(),
        'endpoint': canonical_endpoint(source_endpoint),
    }
    actual_source = {
        'project': str(source.get('project') or '').strip(),
        'database': str(source.get('database') or '').strip(),
        'endpoint': canonical_endpoint(str(source.get('endpoint') or '')),
    }
    if actual_source != expected_source:
        raise SourceWriteFreezeError('freeze lease source authority does not match this migration')
    scopes = lease['scopes']
    if not isinstance(scopes, list) or not scopes or any(not isinstance(item, str) for item in scopes):
        raise SourceWriteFreezeError('freeze lease scopes are invalid')
    if set(scopes) != set(scopes) & SCOPES or not expected_scopes <= set(scopes):
        raise SourceWriteFreezeError('freeze lease does not cover the requested migration scopes')
    issued_at = _parse_time(lease['issued_at'], field='issued_at')
    expires_at = _parse_time(lease['expires_at'], field='expires_at')
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires_at <= issued_at or expires_at - issued_at > timedelta(seconds=MAX_TTL_SECONDS):
        raise SourceWriteFreezeError('freeze lease duration is invalid')
    if current < issued_at or current >= expires_at:
        raise SourceWriteFreezeError('freeze lease is not currently active')
    signature = lease['signature']
    if not isinstance(signature, str) or not _SHA256.fullmatch(signature):
        raise SourceWriteFreezeError('freeze lease signature is invalid')
    payload = dict(lease)
    del payload['signature']
    expected_signature = _signature(payload, secret if secret is not None else os.getenv(SECRET_ENV, ''))
    if not hmac.compare_digest(signature, expected_signature):
        raise SourceWriteFreezeError('freeze lease signature does not verify')
    return {
        'status': 'passed',
        'lease_id': lease_id,
        'holder': holder,
        'source': expected_source,
        'scopes': sorted(set(scopes)),
        'issued_at': lease['issued_at'],
        'expires_at': lease['expires_at'],
    }


def issue_lease(
    path: Path,
    *,
    source_project: str,
    source_database: str,
    source_endpoint: str,
    scopes: list[str],
    holder: str,
    ttl_seconds: int,
    secret: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a new lease without overwriting an existing operator artifact."""

    if path.exists():
        raise SourceWriteFreezeError(f'freeze lease already exists: {path}')
    if not source_project.strip() or not source_database.strip():
        raise SourceWriteFreezeError('source project and database must be explicit')
    if not scopes or set(scopes) != set(scopes) & SCOPES:
        raise SourceWriteFreezeError('lease scopes must be firestore and/or storage')
    if not holder.strip() or len(holder) > 200:
        raise SourceWriteFreezeError('freeze lease holder is invalid')
    if not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise SourceWriteFreezeError(f'freeze lease TTL must be between 1 and {MAX_TTL_SECONDS} seconds')
    issued = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    payload: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION,
        'format': FORMAT,
        'lease_id': str(uuid.uuid4()),
        'status': 'active',
        'holder': holder.strip(),
        'source': {
            'project': source_project.strip(),
            'database': source_database.strip(),
            'endpoint': canonical_endpoint(source_endpoint),
        },
        'scopes': sorted(set(scopes)),
        'issued_at': issued.isoformat().replace('+00:00', 'Z'),
        'expires_at': (issued + timedelta(seconds=ttl_seconds)).isoformat().replace('+00:00', 'Z'),
    }
    payload['signature'] = _signature(payload, secret if secret is not None else os.getenv(SECRET_ENV, ''))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}')
    temporary.write_bytes(_canonical_json(payload) + b'\n')
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    issue = subparsers.add_parser('issue', help='issue an HMAC-signed source-write freeze lease')
    issue.add_argument('--output', required=True, type=Path)
    issue.add_argument('--source-project', required=True)
    issue.add_argument('--source-database', required=True)
    issue.add_argument('--source-endpoint', required=True)
    issue.add_argument('--scope', action='append', required=True, choices=sorted(SCOPES))
    issue.add_argument('--holder', required=True)
    issue.add_argument('--ttl-seconds', type=int, default=3600)
    verify = subparsers.add_parser('verify', help='verify a lease against a source authority')
    verify.add_argument('lease', type=Path)
    verify.add_argument('--source-project', required=True)
    verify.add_argument('--source-database', required=True)
    verify.add_argument('--source-endpoint', required=True)
    verify.add_argument('--scope', action='append', required=True, choices=sorted(SCOPES))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == 'issue':
            payload = issue_lease(
                args.output,
                source_project=args.source_project,
                source_database=args.source_database,
                source_endpoint=args.source_endpoint,
                scopes=args.scope,
                holder=args.holder,
                ttl_seconds=args.ttl_seconds,
            )
            print(
                json.dumps({'status': 'issued', 'lease_id': payload['lease_id'], 'expires_at': payload['expires_at']})
            )
        else:
            print(
                json.dumps(
                    verify_lease(
                        args.lease,
                        source_project=args.source_project,
                        source_database=args.source_database,
                        source_endpoint=args.source_endpoint,
                        required_scopes=set(args.scope),
                    ),
                    sort_keys=True,
                )
            )
    except (SourceWriteFreezeError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
