#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Inventory and reconcile retired Agent VM state without GCE/ADC egress.

Self-hosted deployments deliberately set ``AGENT_VM_PROVIDER=disabled``.  A
historical ``users/{uid}.agentVm`` pointer or ``agentVmMigrations`` child still
blocks account deletion, but a self-host process must not guess that a GCE
resource is gone and must not discover ADC to check it.

This CLI owns the narrow hand-off between those two environments:

* ``inventory`` reads only the local Firestore-compatible authority and writes
  a mode-0600, identity-only inventory.  Secrets such as ``authToken``, IPs,
  and arbitrary user fields never enter the artifact.
* An operator independently checks the listed GCE identities in the managed
  project and supplies a JSON report containing ``status=absent`` for every
  exact resource key.  ``sign-proof`` binds that report to the inventory with
  an HMAC secret; it never calls GCE and is not itself a provider observation.
* ``reconcile`` verifies the signed proof and, only with the explicit
  ``--apply --confirm-agent-vm-state-clear`` pair, clears the exact stale
  pointers/journals in the local authority.  It re-reads the authority and
  uses one transaction per UID, failing closed on any state drift.

No command in this module imports a Compute API client or calls ADC.  Local
inventory/apply additionally require ``FIRESTORE_PG_DSN`` or
``FIRESTORE_EMULATOR_HOST`` so a missing self-host binding cannot fall through
to a real Firestore client backed by ambient credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

# The production Compose command invokes this file directly from the backend
# image. Make the backend package root authoritative independently of cwd.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from google.cloud import firestore

from database._client import get_firestore_client, run_transactional

SCHEMA_VERSION = 1
INVENTORY_FORMAT = 'omi-agent-vm-inventory-v1'
PROOF_FORMAT = 'omi-agent-vm-reconcile-proof-v1'
SECRET_ENV = 'OMI_AGENT_VM_RECONCILE_SECRET'
MAX_PROOF_TTL_SECONDS = 24 * 60 * 60
_NAME = re.compile(r'[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?')
_NUMERIC_ID = re.compile(r'[0-9]+')
_MIGRATION_ID = re.compile(r'[0-9a-f]{24}')
_UID = re.compile(r'^[^/\x00-\x1f\x7f]{1,256}$')


class AgentVmReconcileError(RuntimeError):
    """The inventory/proof/state is absent, invalid, or changed."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _signature(payload: Mapping[str, Any], secret: str | None = None) -> str:
    value = secret if secret is not None else os.getenv(SECRET_ENV, '')
    if not value:
        raise AgentVmReconcileError(f'{SECRET_ENV} must be set')
    return hmac.new(value.encode('utf-8'), _canonical_json(payload), hashlib.sha256).hexdigest()


def _private_new_file(path: Path, content: bytes) -> None:
    if path.exists():
        raise AgentVmReconcileError(f'refusing to overwrite existing artifact: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_private_json(path: Path) -> Any:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise AgentVmReconcileError(f'artifact is not readable: {path}') from error
    if mode & 0o077:
        raise AgentVmReconcileError(f'artifact must be mode 0600 or stricter: {path}')
    try:
        return json.loads(path.read_bytes(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise AgentVmReconcileError(f'artifact is not strict UTF-8 JSON: {path}') from error


def _require_self_host_local_authority() -> Any:
    if os.getenv('AGENT_VM_PROVIDER', '').strip().lower() != 'disabled':
        raise AgentVmReconcileError('AGENT_VM_PROVIDER=disabled is required for local Agent VM reconciliation')
    if not (os.getenv('FIRESTORE_PG_DSN') or os.getenv('FIRESTORE_EMULATOR_HOST')):
        raise AgentVmReconcileError(
            'FIRESTORE_PG_DSN or FIRESTORE_EMULATOR_HOST is required; refusing ambient Firestore/ADC access'
        )
    return get_firestore_client()


def _uid(value: Any) -> str:
    if not isinstance(value, str) or not _UID.fullmatch(value):
        raise AgentVmReconcileError('Agent VM state contains an invalid UID')
    return value


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise AgentVmReconcileError(f'Agent VM state field {field} is invalid')
    return value


def _numeric(value: Any, field: str, *, required: bool = True) -> str:
    if value in (None, '') and not required:
        return ''
    if not isinstance(value, str) or not _NUMERIC_ID.fullmatch(value):
        raise AgentVmReconcileError(f'Agent VM state field {field} is ambiguous')
    return value


def _resource(kind: str, *, name: str, zone: str, instance_id: str) -> dict[str, str]:
    return {
        'key': f'{kind}:{zone}:{name}:{instance_id}',
        'kind': kind,
        'name': name,
        'zone': zone,
        'id': instance_id,
    }


def _agent_vm_projection(
    value: Any, resources: list[dict[str, str]], issues: list[str], prefix: str
) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        issues.append(f'{prefix} must be an object')
        return None
    try:
        name = _name(value.get('vmName'), f'{prefix}.vmName')
        zone = _name(value.get('zone') or 'us-central1-a', f'{prefix}.zone')
        instance_id = _numeric(value.get('instanceId') or value.get('expectedInstanceId'), f'{prefix}.instanceId')
    except AgentVmReconcileError as error:
        issues.append(str(error))
        return None
    resources.append(_resource('instance', name=name, zone=zone, instance_id=instance_id))
    return {'vm_name': name, 'zone': zone, 'instance_id': instance_id}


def _journal_projection(
    value: Any,
    resources: list[dict[str, str]],
    issues: list[str],
    *,
    index: int,
) -> dict[str, Any] | None:
    prefix = f'agentVmMigrations[{index}]'
    if not isinstance(value, Mapping):
        issues.append(f'{prefix} must be an object')
        return None
    try:
        migration_id = value.get('migrationId')
        if not isinstance(migration_id, str) or not _MIGRATION_ID.fullmatch(migration_id):
            raise AgentVmReconcileError(f'{prefix}.migrationId is ambiguous')
        zone = _name(value.get('oldZone'), f'{prefix}.oldZone')
        old_name = _name(value.get('oldVmName'), f'{prefix}.oldVmName')
        old_id = _numeric(value.get('oldInstanceId'), f'{prefix}.oldInstanceId')
        candidate_name = _name(value.get('candidateVmName'), f'{prefix}.candidateVmName')
        candidate_id = _numeric(value.get('candidateInstanceId'), f'{prefix}.candidateInstanceId')
        state_name = _name(value.get('stateDiskName'), f'{prefix}.stateDiskName')
        state_id = _numeric(value.get('stateDiskId'), f'{prefix}.stateDiskId')
        reused = value.get('stateDiskReused')
        if not isinstance(reused, bool):
            raise AgentVmReconcileError(f'{prefix}.stateDiskReused is ambiguous')
        source_name = (
            _name(
                value.get('sourceCloneDiskName'),
                f'{prefix}.sourceCloneDiskName',
            )
            if value.get('sourceCloneDiskName')
            else ''
        )
        source_id = _numeric(value.get('sourceCloneDiskId'), f'{prefix}.sourceCloneDiskId') if source_name else ''
    except AgentVmReconcileError as error:
        issues.append(str(error))
        return None
    resources.extend(
        [
            _resource('instance', name=old_name, zone=zone, instance_id=old_id),
            _resource('instance', name=candidate_name, zone=zone, instance_id=candidate_id),
            _resource('disk', name=state_name, zone=zone, instance_id=state_id),
        ]
    )
    if source_name:
        resources.append(_resource('disk', name=source_name, zone=zone, instance_id=source_id))
    return {
        'migration_id': migration_id,
        'zone': zone,
        'old_vm_name': old_name,
        'old_instance_id': old_id,
        'candidate_vm_name': candidate_name,
        'candidate_instance_id': candidate_id,
        'state_disk_name': state_name,
        'state_disk_id': state_id,
        'state_disk_reused': reused,
        'source_clone_disk_name': source_name,
        'source_clone_disk_id': source_id,
    }


def _late_projection(value: Any, resources: list[dict[str, str]], issues: list[str], uid: str) -> dict[str, str] | None:
    if value is None:
        return None
    prefix = f'account_deletions/{uid}.late_agent_vm_cleanup'
    if not isinstance(value, Mapping):
        issues.append(f'{prefix} must be an object')
        return None
    try:
        name = _name(value.get('vmName'), f'{prefix}.vmName')
        zone = _name(value.get('zone'), f'{prefix}.zone')
        instance_id = _numeric(value.get('expectedInstanceId'), f'{prefix}.expectedInstanceId')
    except AgentVmReconcileError as error:
        issues.append(str(error))
        return None
    resources.append(_resource('instance', name=name, zone=zone, instance_id=instance_id))
    return {'vm_name': name, 'zone': zone, 'instance_id': instance_id}


@dataclass(frozen=True)
class _StateRow:
    uid: str
    user_ref: Any
    user_projection: dict[str, str] | None
    journal_refs: tuple[tuple[Any, dict[str, Any]], ...]
    late_ref: Any | None
    late_projection: dict[str, str] | None


def _row_from_snapshots(
    uid: str,
    user_ref: Any,
    user_data: Mapping[str, Any],
    journal_snapshots: Sequence[Any],
    late_ref: Any,
    late_data: Any,
) -> tuple[dict[str, Any] | None, _StateRow]:
    resources: list[dict[str, str]] = []
    issues: list[str] = []
    user_projection = _agent_vm_projection(user_data.get('agentVm'), resources, issues, 'agentVm')
    journal_refs: list[tuple[Any, dict[str, Any]]] = []
    journal_projections: list[dict[str, Any]] = []
    for index, snapshot in enumerate(journal_snapshots):
        projection = _journal_projection(snapshot.to_dict(), resources, issues, index)
        if projection is not None:
            journal_projections.append(projection)
            journal_refs.append((snapshot.reference, projection))
    late_projection = _late_projection(late_data, resources, issues, uid)
    resources = list({item['key']: item for item in resources}.values())
    record = None
    if user_projection is not None or journal_projections or late_projection is not None or issues:
        record = {
            'uid': uid,
            'agent_vm': user_projection,
            'migration_journals': journal_projections,
            'late_cleanup': late_projection,
            'resources': sorted(resources, key=lambda item: item['key']),
            'issues': sorted(set(issues)),
        }
    return record, _StateRow(
        uid=uid,
        user_ref=user_ref,
        user_projection=user_projection,
        journal_refs=tuple(journal_refs),
        late_ref=late_ref if late_projection is not None else None,
        late_projection=late_projection,
    )


def _collect(client: Any) -> tuple[list[dict[str, Any]], tuple[_StateRow, ...]]:
    records: list[dict[str, Any]] = []
    rows: list[_StateRow] = []
    seen_uids: set[str] = set()
    for user_snapshot in client.collection('users').stream():
        uid = _uid(user_snapshot.id)
        seen_uids.add(uid)
        user_data = user_snapshot.to_dict() or {}
        user_ref = user_snapshot.reference
        journal_snapshots = tuple(user_ref.collection('agentVmMigrations').stream())
        late_ref = client.collection('account_deletions').document(uid)
        late_snapshot = late_ref.get()
        late_data = late_snapshot.get('late_agent_vm_cleanup') if late_snapshot.exists else None
        record, row = _row_from_snapshots(uid, user_ref, user_data, journal_snapshots, late_ref, late_data)
        if record is not None:
            records.append(record)
            rows.append(row)
    # A late cleanup marker can outlive the user document after an earlier
    # wipe attempt. Keep it visible instead of allowing a missing parent to
    # hide the provider state that still blocks reconciliation.
    for deletion_snapshot in client.collection('account_deletions').stream():
        uid = _uid(deletion_snapshot.id)
        if uid in seen_uids or deletion_snapshot.get('late_agent_vm_cleanup') is None:
            continue
        late_ref = deletion_snapshot.reference
        late_data = deletion_snapshot.get('late_agent_vm_cleanup')
        record, row = _row_from_snapshots(
            uid,
            client.collection('users').document(uid),
            {},
            (),
            late_ref,
            late_data,
        )
        if record is not None:
            records.append(record)
            rows.append(row)
    records.sort(key=lambda item: item['uid'])
    rows.sort(key=lambda item: item.uid)
    return records, tuple(rows)


def _inventory_payload(records: Sequence[Mapping[str, Any]], authority_kind: str) -> dict[str, Any]:
    return {
        'schema_version': SCHEMA_VERSION,
        'format': INVENTORY_FORMAT,
        'authority_kind': authority_kind,
        'created_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
        'records': list(records),
    }


def _validate_inventory(value: Any) -> dict[str, Any]:
    required = {'schema_version', 'format', 'authority_kind', 'created_at', 'records'}
    if not isinstance(value, dict) or set(value) != required:
        raise AgentVmReconcileError('inventory has an unsupported schema')
    if value['schema_version'] != SCHEMA_VERSION or value['format'] != INVENTORY_FORMAT:
        raise AgentVmReconcileError('inventory format is unsupported')
    if value['authority_kind'] not in {'firestore_pg', 'firestore_emulator'}:
        raise AgentVmReconcileError('inventory authority kind is unsupported')
    records = value['records']
    if not isinstance(records, list):
        raise AgentVmReconcileError('inventory records must be a list')
    seen_uids: set[str] = set()
    seen_resources: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            'uid',
            'agent_vm',
            'migration_journals',
            'late_cleanup',
            'resources',
            'issues',
        }:
            raise AgentVmReconcileError('inventory record schema is unsupported')
        uid = _uid(record['uid'])
        if uid in seen_uids:
            raise AgentVmReconcileError('inventory contains duplicate UIDs')
        seen_uids.add(uid)
        if not isinstance(record['issues'], list) or any(not isinstance(item, str) for item in record['issues']):
            raise AgentVmReconcileError('inventory issues are malformed')
        resources = record['resources']
        if not isinstance(resources, list):
            raise AgentVmReconcileError('inventory resources must be a list')
        for resource in resources:
            if not isinstance(resource, dict) or set(resource) != {'key', 'kind', 'name', 'zone', 'id'}:
                raise AgentVmReconcileError('inventory resource schema is unsupported')
            key = resource['key']
            if not isinstance(key, str) or key in seen_resources:
                raise AgentVmReconcileError('inventory resource keys must be unique')
            seen_resources.add(key)
    return value


def _proof_payload(
    inventory_path: Path,
    *,
    source_project: str,
    operator: str,
    resources: list[dict[str, str]],
    ttl_seconds: int,
) -> dict[str, Any]:
    if not source_project.strip() or not operator.strip():
        raise AgentVmReconcileError('source project and operator are required')
    if not 1 <= ttl_seconds <= MAX_PROOF_TTL_SECONDS:
        raise AgentVmReconcileError(f'proof TTL must be between 1 and {MAX_PROOF_TTL_SECONDS} seconds')
    issued = datetime.now(timezone.utc).replace(microsecond=0)
    payload: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION,
        'format': PROOF_FORMAT,
        'inventory_sha256': _sha256_file(inventory_path),
        'source_project': source_project.strip(),
        'operator': operator.strip(),
        'issued_at': issued.isoformat().replace('+00:00', 'Z'),
        'expires_at': (issued + timedelta(seconds=ttl_seconds)).isoformat().replace('+00:00', 'Z'),
        'resources': sorted(resources, key=lambda item: item['key']),
    }
    payload['signature'] = _signature(payload)
    return payload


def _verify_proof(inventory_path: Path, proof_path: Path, *, source_project: str) -> dict[str, Any]:
    inventory = _validate_inventory(_read_private_json(inventory_path))
    proof = _read_private_json(proof_path)
    required = {
        'schema_version',
        'format',
        'inventory_sha256',
        'source_project',
        'operator',
        'issued_at',
        'expires_at',
        'resources',
        'signature',
    }
    if not isinstance(proof, dict) or set(proof) != required:
        raise AgentVmReconcileError('reconcile proof schema is unsupported')
    if proof['schema_version'] != SCHEMA_VERSION or proof['format'] != PROOF_FORMAT:
        raise AgentVmReconcileError('reconcile proof format is unsupported')
    if proof['inventory_sha256'] != _sha256_file(inventory_path):
        raise AgentVmReconcileError('reconcile proof does not match this inventory')
    if proof['source_project'] != source_project.strip():
        raise AgentVmReconcileError('reconcile proof source project does not match')
    if not isinstance(proof['operator'], str) or not proof['operator'].strip():
        raise AgentVmReconcileError('reconcile proof operator is invalid')
    try:
        issued = datetime.fromisoformat(str(proof['issued_at']).replace('Z', '+00:00'))
        expires = datetime.fromisoformat(str(proof['expires_at']).replace('Z', '+00:00'))
    except ValueError as error:
        raise AgentVmReconcileError('reconcile proof timestamps are invalid') from error
    if issued.tzinfo is None or expires.tzinfo is None or expires <= issued:
        raise AgentVmReconcileError('reconcile proof timestamps are invalid')
    if expires - issued > timedelta(seconds=MAX_PROOF_TTL_SECONDS):
        raise AgentVmReconcileError('reconcile proof TTL is too long')
    if datetime.now(timezone.utc) >= expires.astimezone(timezone.utc):
        raise AgentVmReconcileError('reconcile proof has expired')
    signature = proof['signature']
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature, _signature({key: value for key, value in proof.items() if key != 'signature'})
    ):
        raise AgentVmReconcileError('reconcile proof signature does not verify')
    expected_keys = sorted(resource['key'] for record in inventory['records'] for resource in record['resources'])
    resources = proof['resources']
    if not isinstance(resources, list) or any(
        not isinstance(item, dict)
        or set(item) != {'key', 'status'}
        or not isinstance(item.get('key'), str)
        or item.get('status') != 'absent'
        for item in resources
    ):
        raise AgentVmReconcileError('reconcile proof resources must all be absent')
    actual_keys = sorted(item['key'] for item in resources)
    if actual_keys != expected_keys:
        raise AgentVmReconcileError('reconcile proof resource set does not match inventory')
    if any(record['issues'] for record in inventory['records']):
        raise AgentVmReconcileError('inventory contains ambiguous state; it cannot be reconciled')
    return proof


def _clear_rows(client: Any, rows: Sequence[_StateRow]) -> None:
    for row in rows:

        @firestore.transactional
        def clear(transaction: Any) -> None:
            user_snapshot = transaction.get(row.user_ref)
            current_user = user_snapshot.get('agentVm') if user_snapshot.exists else None
            resources: list[dict[str, str]] = []
            issues: list[str] = []
            current_projection = _agent_vm_projection(current_user, resources, issues, 'agentVm')
            if issues or current_projection != row.user_projection:
                raise AgentVmReconcileError(f'Agent VM state changed for UID {row.uid}')
            for journal_ref, expected in row.journal_refs:
                snapshot = transaction.get(journal_ref)
                current = _journal_projection(snapshot.to_dict(), [], [], index=0) if snapshot.exists else None
                if current != expected:
                    raise AgentVmReconcileError(f'Agent VM migration journal changed for UID {row.uid}')
            if row.late_ref is not None:
                late_snapshot = transaction.get(row.late_ref)
                current_late = late_snapshot.get('late_agent_vm_cleanup') if late_snapshot.exists else None
                current_projection = _late_projection(current_late, [], [], row.uid)
                if current_projection != row.late_projection:
                    raise AgentVmReconcileError(f'late Agent VM cleanup state changed for UID {row.uid}')
            if row.user_projection is not None:
                transaction.update(row.user_ref, {'agentVm': firestore.DELETE_FIELD})
            for journal_ref, _expected in row.journal_refs:
                transaction.delete(journal_ref)
            if row.late_ref is not None:
                transaction.update(row.late_ref, {'late_agent_vm_cleanup': firestore.DELETE_FIELD})

        run_transactional(client, clear, attempts=3)


def _authority_kind() -> str:
    return 'firestore_pg' if os.getenv('FIRESTORE_PG_DSN') else 'firestore_emulator'


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    inventory = subparsers.add_parser('inventory', help='read local state and write a private typed inventory')
    inventory.add_argument('--output', required=True, type=Path)
    sign = subparsers.add_parser(
        'sign-proof', help='bind an independently collected absent-resource report to inventory'
    )
    sign.add_argument('--inventory', required=True, type=Path)
    sign.add_argument('--resource-report', required=True, type=Path)
    sign.add_argument('--output', required=True, type=Path)
    sign.add_argument('--source-project', required=True)
    sign.add_argument('--operator', required=True)
    sign.add_argument('--ttl-seconds', type=int, default=3600)
    for command in ('verify', 'reconcile'):
        child = subparsers.add_parser(command, help='verify a signed external absence proof')
        child.add_argument('--inventory', required=True, type=Path)
        child.add_argument('--proof', required=True, type=Path)
        child.add_argument('--source-project', required=True)
        if command == 'reconcile':
            child.add_argument('--apply', action='store_true')
            child.add_argument('--confirm-agent-vm-state-clear', action='store_true')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == 'inventory':
            client = _require_self_host_local_authority()
            records, _rows = _collect(client)
            payload = _inventory_payload(records, _authority_kind())
            _private_new_file(args.output, _canonical_json(payload) + b'\n')
            print(
                json.dumps(
                    {'status': 'inventory-written', 'records': len(records), 'sha256': _sha256_file(args.output)}
                )
            )
            return 0
        if args.command == 'sign-proof':
            inventory = _validate_inventory(_read_private_json(args.inventory))
            if any(record['issues'] for record in inventory['records']):
                raise AgentVmReconcileError('inventory contains ambiguous state; it cannot be signed')
            report = _read_private_json(args.resource_report)
            if (
                not isinstance(report, dict)
                or set(report) != {'resources'}
                or not isinstance(report['resources'], list)
            ):
                raise AgentVmReconcileError('resource report must contain only a resources list')
            resources = report['resources']
            if any(
                not isinstance(item, dict)
                or set(item) != {'key', 'status'}
                or not isinstance(item.get('key'), str)
                or item.get('status') != 'absent'
                for item in resources
            ):
                raise AgentVmReconcileError('resource report must mark every resource absent')
            expected = sorted(resource['key'] for record in inventory['records'] for resource in record['resources'])
            if sorted(item['key'] for item in resources) != expected:
                raise AgentVmReconcileError('resource report does not match inventory')
            payload = _proof_payload(
                args.inventory,
                source_project=args.source_project,
                operator=args.operator,
                resources=resources,
                ttl_seconds=args.ttl_seconds,
            )
            _private_new_file(args.output, _canonical_json(payload) + b'\n')
            print(json.dumps({'status': 'proof-written', 'sha256': _sha256_file(args.output)}))
            return 0

        proof = _verify_proof(args.inventory, args.proof, source_project=args.source_project)
        if args.command == 'verify':
            print(
                json.dumps(
                    {'status': 'passed', 'operator': proof['operator'], 'inventory_sha256': proof['inventory_sha256']}
                )
            )
            return 0
        if not args.apply:
            print(json.dumps({'status': 'ready-to-reconcile', 'inventory_sha256': proof['inventory_sha256']}))
            return 0
        if not args.confirm_agent_vm_state_clear:
            raise AgentVmReconcileError('reconcile --apply requires --confirm-agent-vm-state-clear')
        client = _require_self_host_local_authority()
        records, rows = _collect(client)
        inventory = _validate_inventory(_read_private_json(args.inventory))
        if records != inventory['records']:
            raise AgentVmReconcileError('local Agent VM state changed since inventory capture')
        _clear_rows(client, rows)
        remaining, _ = _collect(client)
        if remaining:
            raise AgentVmReconcileError('Agent VM state remains after reconcile')
        print(json.dumps({'status': 'reconciled', 'inventory_sha256': proof['inventory_sha256']}))
        return 0
    except (AgentVmReconcileError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
