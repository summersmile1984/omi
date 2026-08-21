from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from database.vector_projection import (
    PROJECTION_MODEL_KEY,
    VECTOR_PROJECTION_LOGICAL_NAMESPACES,
    ProjectionUnavailableError,
    VersionedVectorStoreAdapter,
    physical_namespace,
)
from database.memory_vector_metadata import canonical_memory_provider_id
from database.vector_store import QdrantVectorStoreAdapter
from scripts.vector_projection_migration import (
    MigrationCliError,
    backfill_from_authority,
    load_authority_records,
    load_receipt,
    main,
    rollback_manifest,
    switch_from_receipt,
    switch_from_plan,
    verify_receipt,
)


class FakeEmbeddings:
    provider_id = 'generic'
    model_id = 'local-embedding'
    dimension = 2

    def __init__(self) -> None:
        self.calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(index + 1), float(len(text))] for index, text in enumerate(texts)]


def _configure_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        'VECTOR_PROJECTION_MODE': 'dual_write',
        'VECTOR_PROJECTION_ACTIVE_VERSION': 'v1',
        'VECTOR_PROJECTION_TARGET_VERSION': 'v2',
        'VECTOR_PROJECTION_SCHEMA_VERSION': '3',
        'VECTOR_PROJECTION_DELETE_VERSIONS': 'v1,v2',
        'EMBEDDING_PROVIDER': 'generic',
        'EMBEDDING_MODEL': 'local-embedding',
        'EMBEDDING_DIMENSION': '2',
        'VECTOR_PROJECTION_REQUIRED_NAMESPACES': 'memories',
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _authority(path: Path, *, second_content: str = 'second memory') -> None:
    records = [
        {'id': 'one', 'content': 'first memory', 'metadata': {'uid': 'u1'}},
        {'id': 'two', 'content': second_content, 'metadata': {'uid': 'u1', 'kind': 'memory'}},
    ]
    path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')


def _canonical_authority(path: Path) -> None:
    metadata = {
        'memory_schema_version': 1,
        'memory_layer': 'long_term',
        'uid': 'u1',
        'memory_id': 'memory-1',
        'status': 'active',
        'processing_state': 'processed',
        'source_state': 'active',
        'visibility': 'private',
        'sensitivity_labels': [],
        'restricted_sensitivity': False,
        'account_generation': 0,
        'item_revision': 2,
        'source_commit_id': 'source-1',
        'content_hash': 'content-1',
        'projection_commit_id': 'ledger-1',
        'vector_updated_at': '2025-01-02T00:00:00+00:00',
    }
    path.write_text(
        json.dumps({'id': 'u1-memory-1', 'content': 'canonical memory', 'metadata': metadata}) + '\n',
        encoding='utf-8',
    )


def _authority_manifest(records_path: Path, *, namespace: str = 'memories', memory_mode: str = 'legacy') -> Path:
    authority_namespace = {'memories': 'ns2'}.get(namespace, namespace)
    records = records_path.read_bytes()
    manifest_path = records_path.with_name('manifest.json')
    manifest_path.write_text(
        json.dumps(
            {
                'format': 'omi-authoritative-vector-export-manifest-v1',
                'export_format': 'omi-authoritative-vector-export-v1',
                'source_kind': 'firestore_pg_facade',
                'source_authority': {
                    'project': 'operator-project',
                    'database': '(default)',
                    'endpoint': 'firestore.operator.example',
                },
                'source_freeze_lease_id': 'lease-1',
                'memory_mode': memory_mode,
                'uid_scope': 'explicit',
                'uids': ['u1'],
                'uid_sha256': hashlib.sha256(b'u1\n').hexdigest(),
                'uid_count': 1,
                'namespace_count': 1,
                'empty_export_acknowledged': False,
                'namespaces': [authority_namespace],
                'files': [
                    {
                        'namespace': authority_namespace,
                        'path': records_path.name,
                        'source_collection': 'memory_items',
                        'record_count': len(records.splitlines()),
                        'sha256': hashlib.sha256(records).hexdigest(),
                    }
                ],
            }
        ),
        encoding='utf-8',
    )
    records_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    manifest_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return manifest_path


def _stores() -> tuple[QdrantVectorStoreAdapter, VersionedVectorStoreAdapter]:
    raw = QdrantVectorStoreAdapter(QdrantClient(location=':memory:'), collection_prefix='migration_cli')
    return raw, VersionedVectorStoreAdapter(raw, FakeEmbeddings())


def test_backfill_verify_switch_and_rollback_are_executable_from_authority_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_migration(monkeypatch)
    records_path = tmp_path / 'memories.jsonl'
    receipt_path = tmp_path / 'memories-v2.receipt.jsonl'
    switch_path = tmp_path / 'switch.env'
    switch_plan_path = tmp_path / 'switch-plan.json'
    rollback_path = tmp_path / 'rollback.env'
    _authority(records_path)
    manifest_path = _authority_manifest(records_path)
    _, store = _stores()
    embeddings = FakeEmbeddings()

    backfill = backfill_from_authority(
        records_path=records_path,
        receipt_path=receipt_path,
        namespace='memories',
        target_version='v2',
        store=store,
        embeddings=embeddings,
        batch_size=1,
        authority_manifest_path=manifest_path,
    )
    receipt = load_receipt(receipt_path)
    assert backfill['status'] == 'backfilled'
    assert backfill['written'] == 2
    assert receipt.header.source_record_count == 2
    assert receipt.header.embedding_provider == 'generic'
    assert receipt.header.embedding_model == 'local-embedding'
    assert receipt.header.embedding_dimension == 2
    assert receipt.header.projection_schema_version == 3

    def store_factory(_identity: object) -> VersionedVectorStoreAdapter:
        return store

    verification = verify_receipt(
        records_path=records_path,
        receipt_path=receipt_path,
        store_factory=store_factory,
        batch_size=1,
        authority_manifest_path=manifest_path,
    )
    assert verification.ready_to_switch is True
    switch_plan_path.write_text(
        json.dumps(
            {
                'format': 'omi-vector-projection-switch-plan-v2',
                'projections': [
                    {
                        'namespace': 'memories',
                        'records': records_path.name,
                        'receipt': receipt_path.name,
                        'manifest': manifest_path.name,
                    }
                ],
            }
        ),
        encoding='utf-8',
    )
    switch_manifest = switch_from_plan(
        plan_path=switch_plan_path,
        env_output=switch_path,
        store_factory=store_factory,
        batch_size=1,
    )
    assert switch_manifest == {
        'VECTOR_PROJECTION_MODE': 'single',
        'VECTOR_PROJECTION_ACTIVE_VERSION': 'v2',
        'VECTOR_PROJECTION_TARGET_VERSION': '',
        'VECTOR_PROJECTION_SCHEMA_VERSION': '3',
        'VECTOR_PROJECTION_DELETE_VERSIONS': 'v1,v2',
    }
    assert switch_path.read_text(encoding='utf-8').splitlines() == [
        'VECTOR_PROJECTION_MODE=single',
        'VECTOR_PROJECTION_ACTIVE_VERSION=v2',
        'VECTOR_PROJECTION_TARGET_VERSION=',
        'VECTOR_PROJECTION_SCHEMA_VERSION=3',
        'VECTOR_PROJECTION_DELETE_VERSIONS=v1,v2',
    ]

    for name, value in switch_manifest.items():
        monkeypatch.setenv(name, value)
    assert rollback_manifest(
        previous_version='v1',
        abandoned_versions=('v2',),
        env_output=rollback_path,
    ) == {
        'VECTOR_PROJECTION_MODE': 'single',
        'VECTOR_PROJECTION_ACTIVE_VERSION': 'v1',
        'VECTOR_PROJECTION_TARGET_VERSION': '',
        'VECTOR_PROJECTION_SCHEMA_VERSION': '3',
        'VECTOR_PROJECTION_DELETE_VERSIONS': 'v1,v2',
    }


def test_ns2_canonical_mode_fences_lineage_and_uses_canonical_provider_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_migration(monkeypatch)
    records_path = tmp_path / 'canonical.jsonl'
    receipt_path = tmp_path / 'canonical.receipt.jsonl'
    _canonical_authority(records_path)
    raw, store = _stores()

    result = backfill_from_authority(
        records_path=records_path,
        receipt_path=receipt_path,
        namespace='ns2',
        target_version='v2',
        store=store,
        embeddings=FakeEmbeddings(),
        batch_size=1,
    )
    assert result['status'] == 'backfilled'
    receipt = load_receipt(receipt_path)
    assert receipt.header.memory_mode == 'canonical'
    expected_id = canonical_memory_provider_id('u1', 'memory-1')
    assert receipt.records[0].id == expected_id
    assert receipt.records[0].metadata['projection_commit_id'] == 'ledger-1'
    actual = raw.fetch(ids=[expected_id], namespace=physical_namespace('ns2', 'v2'))
    assert expected_id in actual['vectors']
    assert actual['vectors'][expected_id]['metadata']['memory_schema_version'] == 1
    verification = verify_receipt(
        records_path=records_path,
        receipt_path=receipt_path,
        store_factory=lambda _identity: store,
        batch_size=1,
    )
    assert verification.ready_to_switch is True


def test_switch_refuses_missing_or_wrong_identity_target_without_writing_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_migration(monkeypatch)
    records_path = tmp_path / 'memories.jsonl'
    receipt_path = tmp_path / 'receipt.jsonl'
    env_output = tmp_path / 'switch.env'
    _authority(records_path)
    raw, store = _stores()
    backfill_from_authority(
        records_path=records_path,
        receipt_path=receipt_path,
        namespace='memories',
        target_version='v2',
        store=store,
        embeddings=FakeEmbeddings(),
        batch_size=10,
    )
    raw.update(
        'one',
        set_metadata={PROJECTION_MODEL_KEY: 'tampered-model'},
        namespace=physical_namespace('memories', 'v2'),
    )
    raw.delete(ids=['two'], namespace=physical_namespace('memories', 'v2'))
    raw.upsert(
        vectors=[{'id': 'unexpected', 'values': [0.0, 1.0], 'metadata': {'uid': 'u1'}}],
        namespace=physical_namespace('memories', 'v2'),
    )

    with pytest.raises(ProjectionUnavailableError, match='target verification is incomplete'):
        switch_from_receipt(
            records_path=records_path,
            receipt_path=receipt_path,
            env_output=env_output,
            store_factory=lambda _identity: store,
            batch_size=10,
        )
    assert not env_output.exists()
    report = verify_receipt(
        records_path=records_path,
        receipt_path=receipt_path,
        store_factory=lambda _identity: store,
        batch_size=10,
    )
    assert report.missing_ids == ('two',)
    assert report.mismatched_ids == ('one',)
    assert report.unexpected_count == 1


def test_verify_rejects_changed_authority_export_even_when_ids_are_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_migration(monkeypatch)
    records_path = tmp_path / 'memories.jsonl'
    receipt_path = tmp_path / 'receipt.jsonl'
    _authority(records_path)
    _, store = _stores()
    backfill_from_authority(
        records_path=records_path,
        receipt_path=receipt_path,
        namespace='memories',
        target_version='v2',
        store=store,
        embeddings=FakeEmbeddings(),
        batch_size=10,
    )
    _authority(records_path, second_content='edited after backfill')

    with pytest.raises(MigrationCliError, match='SHA-256'):
        verify_receipt(
            records_path=records_path,
            receipt_path=receipt_path,
            store_factory=lambda _identity: store,
            batch_size=10,
        )


@pytest.mark.parametrize(
    'lines, message',
    [
        ([{'id': 'one', 'content': 'ok', 'metadata': {}, 'vector': [1, 2]}], 'unknown fields'),
        (
            [
                {'id': 'one', 'content': 'ok', 'metadata': {}},
                {'id': 'one', 'content': 'again', 'metadata': {}},
            ],
            'duplicate id',
        ),
        (
            [{'id': 'one', 'content': 'ok', 'metadata': {'projection_model': 'spoofed'}}],
            'may not set projection fields',
        ),
    ],
)
def test_authority_validation_rejects_ambiguous_or_projection_owned_input(
    tmp_path: Path,
    lines: list[dict[str, object]],
    message: str,
) -> None:
    path = tmp_path / 'invalid.jsonl'
    path.write_text(''.join(json.dumps(line) + '\n' for line in lines), encoding='utf-8')
    with pytest.raises(MigrationCliError, match=message):
        load_authority_records(path)


def test_backfill_requires_explicit_dual_write_contract_before_embedding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_migration(monkeypatch)
    monkeypatch.setenv('VECTOR_PROJECTION_MODE', 'single')
    records_path = tmp_path / 'memories.jsonl'
    receipt_path = tmp_path / 'receipt.jsonl'
    _authority(records_path)
    _, store = _stores()
    embeddings = FakeEmbeddings()

    with pytest.raises(MigrationCliError, match='must be dual_write'):
        backfill_from_authority(
            records_path=records_path,
            receipt_path=receipt_path,
            namespace='memories',
            target_version='v2',
            store=store,
            embeddings=embeddings,
            batch_size=10,
        )
    assert embeddings.calls == 0
    assert not receipt_path.exists()


def test_cli_validate_reports_safe_json_and_never_overwrites_operator_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records_path = tmp_path / 'memories.jsonl'
    _authority(records_path)
    manifest_path = _authority_manifest(records_path)
    assert (
        main(
            [
                'validate',
                '--records',
                str(records_path),
                '--manifest',
                str(manifest_path),
                '--namespace',
                'memories',
                '--memory-mode',
                'legacy',
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'valid'
    assert result['records'] == 2

    _configure_migration(monkeypatch)
    existing_output = tmp_path / 'rollback.env'
    existing_output.write_text('operator-owned\n', encoding='utf-8')
    assert (
        main(
            [
                'rollback',
                '--previous-version',
                'v1',
                '--abandoned-version',
                'v2',
                '--env-output',
                str(existing_output),
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)
    assert error['status'] == 'error'
    assert 'refusing to overwrite' in error['error']
    assert existing_output.read_text(encoding='utf-8') == 'operator-owned\n'


def test_rollback_refuses_implicit_projection_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_migration(monkeypatch)
    monkeypatch.delenv('VECTOR_PROJECTION_ACTIVE_VERSION')
    env_output = tmp_path / 'rollback.env'
    with pytest.raises(MigrationCliError, match='VECTOR_PROJECTION_ACTIVE_VERSION must be explicitly configured'):
        rollback_manifest(
            previous_version='v1',
            abandoned_versions=('v2',),
            env_output=env_output,
        )
    assert not env_output.exists()


def test_switch_plan_must_cover_every_required_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_migration(monkeypatch)
    monkeypatch.setenv('VECTOR_PROJECTION_REQUIRED_NAMESPACES', 'memories,ns1')
    plan_path = tmp_path / 'switch-plan.json'
    plan_path.write_text(
        json.dumps(
            {
                'format': 'omi-vector-projection-switch-plan-v2',
                'projections': [
                    {
                        'namespace': 'memories',
                        'records': 'memories.jsonl',
                        'receipt': 'receipt.jsonl',
                        'manifest': 'manifest.json',
                    }
                ],
            }
        ),
        encoding='utf-8',
    )
    env_output = tmp_path / 'switch.env'
    with pytest.raises(MigrationCliError, match='namespace set does not match'):
        switch_from_plan(
            plan_path=plan_path,
            env_output=env_output,
            store_factory=lambda _identity: _stores()[1],
            batch_size=10,
        )
    assert not env_output.exists()


def test_self_host_required_namespace_manifest_covers_runtime_projection_namespaces() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    env_path = repository_root / 'deploy' / 'self-host' / '.env.production.example'
    configured = next(
        line.split('=', 1)[1]
        for line in env_path.read_text(encoding='utf-8').splitlines()
        if line.startswith('VECTOR_PROJECTION_REQUIRED_NAMESPACES=')
    )
    assert set(configured.split(',')) == set(VECTOR_PROJECTION_LOGICAL_NAMESPACES)


def test_empty_authority_export_requires_explicit_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_migration(monkeypatch)
    records_path = tmp_path / 'empty.jsonl'
    records_path.write_text('', encoding='utf-8')
    receipt_path = tmp_path / 'empty.receipt.jsonl'
    _, store = _stores()

    with pytest.raises(MigrationCliError, match='contains no records'):
        load_authority_records(records_path)
    backfill_from_authority(
        records_path=records_path,
        receipt_path=receipt_path,
        namespace='memories',
        target_version='v2',
        store=store,
        embeddings=FakeEmbeddings(),
        batch_size=10,
        allow_empty=True,
    )
    receipt = load_receipt(receipt_path)
    assert receipt.header.empty_export_acknowledged is True
    assert receipt.records == ()
    report = verify_receipt(
        records_path=records_path,
        receipt_path=receipt_path,
        store_factory=lambda _identity: store,
        batch_size=10,
    )
    assert report.ready_to_switch is True
