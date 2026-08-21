from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.export_authoritative_vectors import (
    ExportError,
    SourceDocument,
    export_authoritative_vectors,
    verify_authoritative_export_manifest,
)


class FakeAuthority:
    source_kind = 'fake_firestore_pg_facade'

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, list[SourceDocument]]] = {
            'u2': {
                'conversations': [],
                'memory_items': [],
                'workstreams': [],
                'x_posts': [],
                'screen_activity': [],
                'action_items': [],
            },
            'u1': {
                'conversations': [
                    SourceDocument(
                        'c1',
                        {
                            'structured': {'title': 'A title', 'overview': 'A summary'},
                            'created_at': datetime(2025, 1, 1, tzinfo=timezone.utc),
                            'transcript_segments': [{'text': 'hello', 'is_user': True}],
                        },
                    ),
                    SourceDocument('locked', {'is_locked': True, 'structured': {'overview': 'secret'}}),
                ],
                'memory_items': [
                    SourceDocument(
                        'm1',
                        {
                            'memory_id': 'm1',
                            'uid': 'u1',
                            'status': 'active',
                            'content': 'canonical memory',
                            'tier': 'long_term',
                            'updated_at': datetime(2025, 1, 2, tzinfo=timezone.utc),
                        },
                    ),
                    SourceDocument('m2', {'status': 'superseded', 'content': 'old memory'}),
                ],
                'workstreams': [
                    SourceDocument(
                        'w1',
                        {
                            'workstream_id': 'w1',
                            'status': 'open',
                            'objective': 'Ship it',
                            'current_state_summary': 'Testing',
                            'account_generation': 3,
                        },
                    )
                ],
                'x_posts': [SourceDocument('p1', {'id': 'p1', 'text': 'A post', 'kind': 'bookmark'})],
                'screen_activity': [
                    SourceDocument(
                        's1',
                        {
                            'storageId': 's1',
                            'ocrText': 'OCR text',
                            # The production screen_activity writer stores a
                            # UTC wall-clock timestamp string, not epoch text.
                            'timestamp': '2025-01-01 00:00:00.000',
                        },
                    )
                ],
                'action_items': [
                    SourceDocument('a1', {'id': 'a1', 'description': 'Do this', 'status': 'active'}),
                    SourceDocument('a2', {'description': 'Deleted', 'deleted': True}),
                ],
            },
        }

    def list_uids(self):
        return self.docs.keys()

    def iter_user_documents(self, uid: str, collection: str):
        return iter(self.docs.get(uid, {}).get(collection, []))


def test_export_writes_all_seven_authority_namespaces_and_hash_bound_sidecar(tmp_path: Path):
    manifest = export_authoritative_vectors(
        FakeAuthority(),
        uids=None,
        all_users=True,
        namespaces=None,
        output_dir=tmp_path / 'export',
        allow_empty=True,
        memory_mode='legacy',
    )

    assert manifest['source_kind'] == 'fake_firestore_pg_facade'
    assert manifest['memory_mode'] == 'legacy'
    assert manifest['uid_scope'] == 'all_users'
    assert manifest['uids'] == ['u1', 'u2']
    assert manifest['uid_sha256'] == hashlib.sha256(b'u1\nu2\n').hexdigest()
    assert manifest['uid_count'] == 2
    assert manifest['namespaces'] == [
        'ns1',
        'ns2',
        'workstream-association-v1',
        'ns_x',
        'ns3',
        'ns4',
        'ns_tchunks',
    ]
    records = {
        item['namespace']: [json.loads(line) for line in (tmp_path / 'export' / item['path']).read_text().splitlines()]
        for item in manifest['files']
    }
    assert records['ns1'][0]['id'] == 'u1-c1'
    assert records['ns1'][0]['content'] == 'A title (Other)\nA summary'
    assert records['ns2'][0]['id'] == 'u1-m1'
    assert records['workstream-association-v1'][0]['id'] == 'u1:workstream:3:w1'
    assert records['ns_x'][0]['id'] == 'u1-x-p1'
    assert records['ns3'][0]['id'] == 'u1-sa-s1'
    assert records['ns3'][0]['metadata']['timestamp'] == 1735689600
    assert records['ns4'][0]['id'] == 'u1-ai-a1'
    assert records['ns_tchunks'][0]['id'] == 'u1-c1-c0'
    assert all(set(record) == {'id', 'content', 'metadata'} for rows in records.values() for record in rows)
    assert all(record['id'] != 'u1-locked' for record in records['ns1'])
    assert all(record['id'] != 'u1-m2' for record in records['ns2'])
    assert all(record['id'] != 'u1-ai-a2' for record in records['ns4'])

    sidecar = json.loads((tmp_path / 'export' / 'manifest.json').read_text())
    assert sidecar == manifest
    assert stat.S_IMODE((tmp_path / 'export').stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / 'export' / 'manifest.json').stat().st_mode) == 0o600
    for item in manifest['files']:
        payload = (tmp_path / 'export' / item['path']).read_bytes()
        assert item['record_count'] == len(payload.splitlines())
        assert item['sha256'] == hashlib.sha256(payload).hexdigest()
        assert stat.S_IMODE((tmp_path / 'export' / item['path']).stat().st_mode) == 0o600


def test_export_refuses_duplicate_uids_and_existing_output(tmp_path: Path):
    with pytest.raises(ExportError, match='duplicate UID'):
        export_authoritative_vectors(
            FakeAuthority(),
            uids=['u1', 'u1'],
            all_users=False,
            namespaces=['ns2'],
            output_dir=tmp_path / 'one',
        )

    output = tmp_path / 'two'
    output.mkdir(mode=0o700)
    output.chmod(0o700)
    (output / 'leftover').write_text('do not replace')
    with pytest.raises(ExportError, match='new or empty'):
        export_authoritative_vectors(
            FakeAuthority(),
            uids=['u1'],
            all_users=False,
            namespaces=['ns2'],
            output_dir=output,
        )


def test_export_manifest_preflight_binds_pg_authority_and_rejects_tampering(tmp_path: Path):
    authority = FakeAuthority()
    authority.source_kind = 'firestore_pg_facade'
    output = tmp_path / 'export'
    manifest = export_authoritative_vectors(
        authority,
        uids=['u1'],
        all_users=False,
        namespaces=['ns2'],
        output_dir=output,
        allow_empty=True,
        memory_mode='legacy',
        source_authority={
            'project': 'operator-project',
            'database': '(default)',
            'endpoint': 'firestore.operator.example',
        },
        source_freeze_lease_id='lease-1',
    )
    records_path = output / 'ns2.jsonl'
    evidence = verify_authoritative_export_manifest(
        output / 'manifest.json',
        records_path=records_path,
        namespace='ns2',
        memory_mode='legacy',
    )
    assert evidence['manifest_sha256'] == hashlib.sha256((output / 'manifest.json').read_bytes()).hexdigest()
    assert evidence['source_kind'] == 'firestore_pg_facade'
    records_path.write_bytes(records_path.read_bytes() + b'{}\n')
    with pytest.raises(ExportError, match='count/hash'):
        verify_authoritative_export_manifest(
            output / 'manifest.json',
            records_path=records_path,
            namespace='ns2',
            memory_mode='legacy',
        )


def test_export_refuses_permissive_output_directory(tmp_path: Path):
    output = tmp_path / 'permissive'
    output.mkdir(mode=0o755)
    with pytest.raises(ExportError, match='private mode'):
        export_authoritative_vectors(
            FakeAuthority(),
            uids=['u1'],
            all_users=False,
            namespaces=['ns2'],
            output_dir=output,
            memory_mode='legacy',
        )


def test_export_removes_partial_files_when_a_later_namespace_fails(tmp_path: Path):
    authority = FakeAuthority()
    authority.docs['u1']['memory_items'].append(
        SourceDocument('broken', {'status': 'active', 'content': 'not serializable', 'tier': object()})
    )
    output = tmp_path / 'partial'
    with pytest.raises(ExportError, match='unsupported authoritative metadata'):
        export_authoritative_vectors(
            authority,
            uids=['u1'],
            all_users=False,
            namespaces=['ns1', 'ns2'],
            output_dir=output,
            memory_mode='legacy',
        )
    assert not output.exists()


def test_export_refuses_undecoded_transcript_instead_of_emitting_empty_projection(tmp_path: Path):
    authority = FakeAuthority()
    authority.docs['u1']['conversations'].append(SourceDocument('encoded', {'transcript_segments': b'ciphertext'}))
    with pytest.raises(ExportError, match='not decoded'):
        export_authoritative_vectors(
            authority,
            uids=['u1'],
            all_users=False,
            namespaces=['ns_tchunks'],
            output_dir=tmp_path / 'encoded',
        )


def test_export_fails_closed_on_empty_namespace_without_explicit_ack(tmp_path: Path):
    with pytest.raises(ExportError, match='empty'):
        export_authoritative_vectors(
            FakeAuthority(),
            uids=['u2'],
            all_users=False,
            namespaces=['ns2'],
            output_dir=tmp_path / 'empty',
        )


def test_export_rechecks_source_freeze_before_each_lazy_source_read(tmp_path: Path):
    checks: list[int] = []

    def guard() -> None:
        checks.append(1)

    export_authoritative_vectors(
        FakeAuthority(),
        uids=['u1'],
        all_users=False,
        namespaces=['ns2'],
        output_dir=tmp_path / 'guarded',
        memory_mode='legacy',
        source_read_guard=guard,
    )

    # The guard runs before the collection iterator advances, including the
    # final StopIteration check; a one-time CLI check is not sufficient for a
    # long export whose lease can expire mid-stream.
    assert len(checks) >= 3


def test_canonical_memory_export_requires_lineage_and_emits_parser_metadata(tmp_path: Path):
    authority = FakeAuthority()
    with pytest.raises(ExportError, match='missing canonical memory lineage'):
        export_authoritative_vectors(
            authority,
            uids=['u1'],
            all_users=False,
            namespaces=['ns2'],
            output_dir=tmp_path / 'missing-lineage',
        )

    authority.docs['u1']['memory_items'][0].data.update(
        {
            'processing_state': 'processed',
            'source_state': 'active',
            'visibility': 'private',
            'sensitivity_labels': [],
            'source_commit_id': 'source-1',
            'content_hash': 'hash-1',
            'ledger_commit_id': 'ledger-1',
            'item_revision': 1,
            'account_generation': 0,
        }
    )
    manifest = export_authoritative_vectors(
        authority,
        uids=['u1'],
        all_users=False,
        namespaces=['ns2'],
        output_dir=tmp_path / 'canonical',
    )
    record = json.loads((tmp_path / 'canonical' / 'ns2.jsonl').read_text().strip())
    assert manifest['memory_mode'] == 'canonical'
    assert record['metadata']['memory_schema_version'] == 1
    assert record['metadata']['projection_commit_id'] == 'ledger-1'
    assert record['metadata']['vector_updated_at'].endswith('+00:00')
