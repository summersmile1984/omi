from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.export_authoritative_vectors import ExportError, SourceDocument, export_authoritative_vectors


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
    )

    assert manifest['source_kind'] == 'fake_firestore_pg_facade'
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
    assert records['ns1'][0]['content'] == 'A summary'
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
    for item in manifest['files']:
        payload = (tmp_path / 'export' / item['path']).read_bytes()
        assert item['record_count'] == len(payload.splitlines())
        assert item['sha256'] == hashlib.sha256(payload).hexdigest()


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
    output.mkdir()
    (output / 'leftover').write_text('do not replace')
    with pytest.raises(ExportError, match='new or empty'):
        export_authoritative_vectors(
            FakeAuthority(),
            uids=['u1'],
            all_users=False,
            namespaces=['ns2'],
            output_dir=output,
        )


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
