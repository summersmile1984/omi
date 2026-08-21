from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from scripts import agent_vm_reconcile as reconcile


def _inventory(tmp_path: Path) -> Path:
    path = tmp_path / 'agent-vm-inventory.json'
    resource = reconcile._resource('instance', name='omi-agent-a', zone='us-central1-a', instance_id='101')
    payload = reconcile._inventory_payload(
        [
            {
                'uid': 'user-a',
                'agent_vm': {'vm_name': 'omi-agent-a', 'zone': 'us-central1-a', 'instance_id': '101'},
                'migration_journals': [],
                'late_cleanup': None,
                'resources': [resource],
                'issues': [],
            }
        ],
        'firestore_pg',
    )
    path.write_bytes(reconcile._canonical_json(payload) + b'\n')
    os.chmod(path, 0o600)
    return path


def test_inventory_projection_excludes_agent_vm_secrets() -> None:
    resources: list[dict[str, str]] = []
    issues: list[str] = []
    projection = reconcile._agent_vm_projection(
        {
            'vmName': 'omi-agent-a',
            'zone': 'us-central1-a',
            'instanceId': '101',
            'authToken': 'must-not-leak',
            'ip': '192.0.2.10',
        },
        resources,
        issues,
        'agentVm',
    )

    assert projection == {'vm_name': 'omi-agent-a', 'zone': 'us-central1-a', 'instance_id': '101'}
    assert 'authToken' not in json.dumps(projection)
    assert '192.0.2.10' not in json.dumps(projection)
    assert not issues


def test_signed_proof_binds_exact_inventory_and_resource_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = _inventory(tmp_path)
    report = tmp_path / 'resource-report.json'
    report.write_text(
        json.dumps(
            {
                'resources': [
                    {
                        'key': 'instance:us-central1-a:omi-agent-a:101',
                        'status': 'absent',
                    }
                ]
            }
        ),
        encoding='utf-8',
    )
    os.chmod(report, 0o600)
    monkeypatch.setenv(reconcile.SECRET_ENV, 'operator-proof-secret')
    proof = reconcile._proof_payload(
        inventory,
        source_project='managed-project',
        operator='change-123',
        resources=json.loads(report.read_text(encoding='utf-8'))['resources'],
        ttl_seconds=300,
    )
    proof_path = tmp_path / 'proof.json'
    proof_path.write_bytes(reconcile._canonical_json(proof) + b'\n')
    os.chmod(proof_path, 0o600)

    verified = reconcile._verify_proof(inventory, proof_path, source_project='managed-project')

    assert verified['operator'] == 'change-123'
    assert stat.S_IMODE(proof_path.stat().st_mode) == 0o600


def test_proof_rejects_inventory_drift_and_foreign_resource(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    monkeypatch.setenv(reconcile.SECRET_ENV, 'operator-proof-secret')
    proof = reconcile._proof_payload(
        inventory,
        source_project='managed-project',
        operator='change-123',
        resources=[{'key': 'instance:us-central1-a:omi-agent-a:101', 'status': 'absent'}],
        ttl_seconds=300,
    )
    proof['resources'] = [{'key': 'instance:us-central1-a:foreign:999', 'status': 'absent'}]
    proof['signature'] = reconcile._signature({key: value for key, value in proof.items() if key != 'signature'})
    proof_path = tmp_path / 'proof.json'
    proof_path.write_bytes(reconcile._canonical_json(proof) + b'\n')
    os.chmod(proof_path, 0o600)

    with pytest.raises(reconcile.AgentVmReconcileError, match='resource set'):
        reconcile._verify_proof(inventory, proof_path, source_project='managed-project')


def test_local_authority_guard_refuses_ambient_adc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('AGENT_VM_PROVIDER', 'disabled')
    monkeypatch.delenv('FIRESTORE_PG_DSN', raising=False)
    monkeypatch.delenv('FIRESTORE_EMULATOR_HOST', raising=False)

    with pytest.raises(reconcile.AgentVmReconcileError, match='refusing ambient Firestore/ADC'):
        reconcile._require_self_host_local_authority()
