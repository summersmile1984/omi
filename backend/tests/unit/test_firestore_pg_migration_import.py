from __future__ import annotations

import ast
import json
import math
import os
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.cloud.firestore_v1 import GeoPoint
from sqlalchemy import create_engine

from database.memory_collections import MemoryCollections
import firestore_pg.client as client_module
from firestore_pg import importer, migrations
from firestore_pg.client import Client
from firestore_pg.codec import (
    FirestoreReferenceValue,
    decode_stored_document,
    decode_value,
    encode_document,
    encode_value,
)
from firestore_pg.engine import _dsn_host
from firestore_pg.importer import capture_source, walk_source
from firestore_pg.migrations import (
    LEGACY_RAW_COLLECTION_IDS_V1,
    STATIC_HASHED_COLLECTION_IDS_V2,
    SchemaNotCurrent,
    _legacy_tables_with_rows,
    collection_table_name,
    validate_collection_id,
)
from scripts import firestore_pg_migrate
from scripts.validate_migration_test_targets import UnsafeMigrationTarget, validate_external_targets


def test_migration_entrypoint_resolves_backend_packages_from_any_working_directory(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'firestore_pg_migrate.py'

    result = subprocess.run(
        [sys.executable, str(script), '--help'],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert 'Explicit schema and Firestore import owner' in result.stdout


def test_firestore_import_rejects_non_private_source_credentials(tmp_path: Path) -> None:
    credentials = tmp_path / 'source-credentials.json'
    credentials.write_text('{}', encoding='utf-8')
    credentials.chmod(0o644)
    args = SimpleNamespace(
        source_project='operator-firestore',
        source_database='(default)',
        source_credentials=credentials,
        source_endpoint='https://firestore.googleapis.com',
    )

    with pytest.raises(ValueError, match='credentials.*0600'):
        firestore_pg_migrate._source_client(args)


class _Snapshot:
    def __init__(self, reference, data):
        self.reference = reference
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return self._data


class _Document:
    def __init__(self, path, data=None, children=()):
        self.path = path
        self._data = data
        self._children = list(children)

    def get(self):
        return _Snapshot(self, self._data)

    def collections(self):
        return iter(self._children)


class _Collection:
    def __init__(self, collection_id, documents):
        self.id = collection_id
        self._documents = list(documents)
        self.show_missing_calls = []

    def list_documents(self, *, show_missing=False):
        self.show_missing_calls.append(show_missing)
        return iter(self._documents)


class _Source:
    def __init__(self, collections):
        self._collections = list(collections)
        self.project = 'unit-source-project'
        self._database = '(default)'
        self._target = 'firestore.googleapis.com'
        self._emulator_host = None

    def collections(self):
        return iter(self._collections)


def _source():
    nested = _Collection('nested_unknown', [_Document('roots/missing/nested_unknown/child', {'v': 2})])
    roots = _Collection(
        'roots',
        [
            _Document('roots/present', {'v': 1}),
            # A missing parent still owns a real nested document.
            _Document('roots/missing', None, [nested]),
        ],
    )
    return _Source([roots])


def test_firestore_import_client_uses_declared_source_endpoint(monkeypatch):
    observed = {}

    def fake_client(**kwargs):
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(firestore_pg_migrate.cloud_firestore, 'Client', fake_client)
    args = SimpleNamespace(
        source_project='operator-firestore',
        source_database='(default)',
        source_credentials=None,
        source_endpoint='https://FIRESTORE.OPERATOR.EXAMPLE:8443/',
    )

    assert firestore_pg_migrate._source_client(args) is not None
    assert observed['project'] == 'operator-firestore'
    assert observed['client_options'].api_endpoint == 'firestore.operator.example:8443'
    assert observed['database'] == '(default)'


def test_walk_source_keeps_missing_parent_nested_document_path():
    source = _source()
    records = list(walk_source(source))

    assert [item['path'] for item in records] == [
        'roots/missing/nested_unknown/child',
        'roots/present',
    ]
    assert {item['collection_id'] for item in records} == {'roots', 'nested_unknown'}
    root_collection = source._collections[0]
    nested_collection = root_collection._documents[1]._children[0]
    assert root_collection.show_missing_calls == [True]
    assert nested_collection.show_missing_calls == [True]


def test_capture_source_writes_private_resume_manifest(tmp_path):
    checkpoint_path = tmp_path / 'checkpoint.json'
    checkpoint = capture_source(_source(), checkpoint_path)
    manifest = Path(checkpoint['manifest'])

    assert checkpoint['status'] == 'captured'
    assert checkpoint['source_count'] == 2
    assert checkpoint['collections'] == ['nested_unknown', 'roots']
    assert checkpoint['source_project'] == 'unit-source-project'
    assert checkpoint['source_database'] == '(default)'
    assert checkpoint['source_resolved_endpoint'] == 'firestore.googleapis.com'
    assert checkpoint['source_emulator_authority'] == ''
    assert checkpoint_path.stat().st_mode & 0o777 == 0o600
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert len(manifest.read_text(encoding='utf-8').splitlines()) == 2


def test_capture_source_rechecks_freeze_and_cleans_partial_manifest(tmp_path):
    checkpoint_path = tmp_path / 'checkpoint.json'
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError('freeze lease expired during source capture')

    with pytest.raises(RuntimeError, match='freeze lease expired'):
        capture_source(_source(), checkpoint_path, source_read_guard=guard)

    assert calls == 3
    assert not checkpoint_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_checkpoint_write_does_not_follow_stale_temp_symlink(tmp_path):
    checkpoint_path = tmp_path / 'checkpoint.json'
    outside = tmp_path / 'outside.json'
    outside.write_text('keep\n', encoding='utf-8')
    stale_temp = tmp_path / f'.{checkpoint_path.name}.tmp-{os.getpid()}'
    stale_temp.symlink_to(outside)

    importer._atomic_json(checkpoint_path, {'status': 'captured'})

    assert outside.read_text(encoding='utf-8') == 'keep\n'
    assert json.loads(checkpoint_path.read_text(encoding='utf-8')) == {'status': 'captured'}
    assert checkpoint_path.stat().st_mode & 0o777 == 0o600


def test_capture_hash_is_independent_of_depth_first_versus_path_order(monkeypatch, tmp_path):
    records = [
        {'path': 'roots/a', 'data': {'v': 1}, 'collection_id': 'roots'},
        {'path': 'roots/a/nested/x', 'data': {'v': 2}, 'collection_id': 'nested'},
        {'path': 'roots/a!', 'data': {'v': 3}, 'collection_id': 'roots'},
    ]
    monkeypatch.setattr(importer, 'walk_source', lambda _client: iter(records))

    checkpoint = capture_source(_Source([]), tmp_path / 'checkpoint.json')
    path_sorted = sorted(records, key=lambda record: record['path'])

    assert checkpoint['source_content_hash'] == importer._inventory(path_sorted).content_hash
    assert importer._inventory(records).content_hash == importer._inventory(reversed(records)).content_hash


def test_import_resumes_after_last_checkpoint_and_reconciles(monkeypatch, tmp_path):
    records = list(importer.walk_source(_source()))
    store = {}
    fail = {'once': True}

    def write(_engine, _tables, record):
        path = record['path']
        if path == 'roots/present' and fail['once']:
            fail['once'] = False
            raise RuntimeError('controlled interruption')
        store[path] = dict(record['data'])

    def inventory(_engine=None):
        target_records = [importer._stored_record(path, data) for path, data in sorted(store.items())]
        return importer._inventory(target_records)

    monkeypatch.setattr(importer, 'check_schema', lambda _engine=None: None)
    monkeypatch.setattr(importer, 'provision_collections', lambda _names, _engine=None: None)
    monkeypatch.setattr(importer, 'target_inventory', inventory)
    monkeypatch.setattr(importer, 'walk_source', lambda _client: iter(records))
    monkeypatch.setattr(
        importer, '_collection_tables', lambda _engine: {'roots': 'roots', 'nested_unknown': 'nested_unknown'}
    )
    monkeypatch.setattr(importer, '_write_manifest_record', write)

    freeze_checks = 0

    def freeze_guard() -> None:
        nonlocal freeze_checks
        freeze_checks += 1

    checkpoint_path = tmp_path / 'checkpoint.json'
    source_client = SimpleNamespace(
        project='unit-source-project',
        _database='(default)',
        _target='firestore.googleapis.com',
        _emulator_host=None,
    )
    with pytest.raises(RuntimeError, match='controlled interruption'):
        importer.run_import(
            source_client,
            checkpoint_path,
            engine=object(),
            checkpoint_interval=1,
            freeze_guard=freeze_guard,
        )
    failed = json.loads(checkpoint_path.read_text(encoding='utf-8'))
    assert failed['status'] == 'failed'
    assert failed['next_index'] == 1

    result = importer.run_import(
        source_client,
        checkpoint_path,
        engine=object(),
        checkpoint_interval=1,
        freeze_guard=freeze_guard,
    )

    assert result['status'] == 'passed'
    assert result['source_count'] == result['target_count'] == 2
    assert result['source_content_hash'] == result['target_content_hash']
    assert sorted(store) == ['roots/missing/nested_unknown/child', 'roots/present']
    # The lease is checked before capture and before every source item in both
    # the initial capture and the final live reconciliation, not just once per
    # phase. This protects long-running migrations from a mid-scan expiry.
    assert freeze_checks >= 6


def test_import_resume_rejects_a_different_source_authority(monkeypatch, tmp_path):
    checkpoint_path = tmp_path / 'checkpoint.json'
    importer.capture_source(_source(), checkpoint_path)
    monkeypatch.setattr(importer, 'check_schema', lambda _engine=None: None)
    monkeypatch.setattr(importer, 'target_inventory', lambda _engine=None: importer.Inventory(0, '', ()))
    other = SimpleNamespace(
        project='other-source-project',
        _database='(default)',
        _target='firestore.googleapis.com',
        _emulator_host=None,
    )

    with pytest.raises(importer.ImportReconciliationError, match='project/database/endpoint/emulator authority'):
        importer.run_import(other, checkpoint_path, engine=object())


def test_import_resume_rejects_endpoint_or_emulator_authority_change(monkeypatch, tmp_path):
    checkpoint_path = tmp_path / 'checkpoint.json'
    importer.capture_source(_source(), checkpoint_path)
    monkeypatch.setattr(importer, 'check_schema', lambda _engine=None: None)
    monkeypatch.setattr(importer, 'target_inventory', lambda _engine=None: importer.Inventory(0, '', ()))
    redirected = SimpleNamespace(
        project='unit-source-project',
        _database='(default)',
        _target='127.0.0.1:18080',
        _emulator_host='127.0.0.1:18080',
    )

    with pytest.raises(importer.ImportReconciliationError, match='endpoint/emulator authority'):
        importer.run_import(redirected, checkpoint_path, engine=object())


def test_import_resume_rejects_non_private_checkpoint_and_manifest(monkeypatch, tmp_path):
    checkpoint_path = tmp_path / 'checkpoint.json'
    importer.capture_source(_source(), checkpoint_path)
    monkeypatch.setattr(importer, 'check_schema', lambda _engine=None: None)
    monkeypatch.setattr(importer, 'target_inventory', lambda _engine=None: importer.Inventory(0, '', ()))

    checkpoint_path.chmod(0o644)
    with pytest.raises(importer.ImportReconciliationError, match='checkpoint.*0600'):
        importer.run_import(_source(), checkpoint_path, engine=object())

    checkpoint_path.chmod(0o600)
    manifest_path = Path(json.loads(checkpoint_path.read_text(encoding='utf-8'))['manifest'])
    manifest_path.chmod(0o644)
    with pytest.raises(importer.ImportReconciliationError, match='manifest.*0600'):
        importer.run_import(_source(), checkpoint_path, engine=object())


def test_runtime_client_checks_schema_instead_of_running_ddl(monkeypatch):
    calls = []
    monkeypatch.setattr(client_module, 'check_schema', lambda: calls.append('check'))

    Client(project='test')

    assert calls == ['check']


def test_compat_install_replaces_parent_package_cached_firestore_module():
    result = subprocess.run(
        [
            sys.executable,
            '-c',
            (
                'from google.cloud import firestore as real; '
                'from firestore_pg.compat import install; install(); '
                'from google.cloud import firestore; '
                'assert firestore is not real; '
                'assert firestore.Client.__module__ == "firestore_pg.client"'
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0, result.stderr


def test_dynamic_collection_ids_map_to_deterministic_safe_tables():
    assert validate_collection_id('future_collection_2') == 'future_collection_2'
    assert collection_table_name('users') == 'users'
    mapped = collection_table_name('Mixed-Case-集合')
    assert mapped == collection_table_name('Mixed-Case-集合')
    assert mapped.startswith('f_')
    assert len(mapped) <= 63
    # Even a valid collection deliberately named after another ID's physical
    # table is hashed again, so registry uniqueness cannot reject valid input.
    assert collection_table_name(mapped) != mapped
    assert set(LEGACY_RAW_COLLECTION_IDS_V1) >= {'users', 'conversations', 'memory_items'}
    with pytest.raises(ValueError, match='invalid Firestore collection ID'):
        validate_collection_id('invalid/path')


def test_schema_v2_provisions_production_control_collections_with_hashed_tables():
    required_controls = {
        'account_deletions',
        'account_deletion_receipts',
        'conversation_finalization_projection_shards',
        'conversation_recovery_state',
    }

    assert required_controls <= STATIC_HASHED_COLLECTION_IDS_V2
    assert required_controls <= set(migrations.known_collections())
    assert all(collection_table_name(collection_id).startswith('f_') for collection_id in required_controls)
    assert 'self_host_live_rows' not in migrations.known_collections()


def test_production_static_collection_references_are_in_versioned_inventory():
    backend_root = Path(__file__).resolve().parents[2]
    repository_root = backend_root.parent
    production_roots = ('database', 'jobs', 'services', 'routers', 'utils')
    declared = set(migrations.known_collections())
    discovered: set[str] = set()

    def logical_collection_id(reference: str) -> str:
        parts = [part for part in reference.split('/') if part]
        assert parts, f'invalid empty Firestore reference in production inventory: {reference!r}'
        return parts[-1] if len(parts) % 2 == 1 else parts[-2]

    source_paths = [
        source_path
        for root_name in production_roots
        for source_path in (backend_root / root_name).rglob('*.py')
        if source_path.name != 'chat_first_e2e_fixture.py'
    ]
    source_paths.append(repository_root / 'deploy' / 'self-host' / 'live-replacement-smoke.py')

    for source_path in source_paths:
        tree = ast.parse(source_path.read_text())
        string_bindings: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                names = [node.target.id] if isinstance(node.target, ast.Name) else []
                value = node.value
            else:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                string_bindings.update({name: value.value for name in names})
                if any(
                    'collection' in name.lower() and 'max' not in name.lower() and 'env' not in name.lower()
                    for name in names
                ):
                    discovered.add(logical_collection_id(value.value))

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {'collection', 'collection_group'}
                and node.args
            ):
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                discovered.add(logical_collection_id(argument.value))
            elif isinstance(argument, ast.Name) and argument.id in string_bindings:
                discovered.add(logical_collection_id(string_bindings[argument.id]))

    memory_collections = MemoryCollections(uid='inventory-user')
    for name, descriptor in vars(MemoryCollections).items():
        if not isinstance(descriptor, property):
            continue
        value = getattr(memory_collections, name)
        if isinstance(value, str):
            discovered.add(logical_collection_id(value))

    assert discovered <= declared, f'unversioned production Firestore collections: {sorted(discovered - declared)}'


def test_tagged_codec_preserves_iso_text_timestamp_bytes_geo_and_reference():
    timestamp = datetime(2026, 8, 21, 12, 34, tzinfo=timezone.utc)
    point = GeoPoint(31.2304, 121.4737)
    value = {
        'iso_text': '2026-08-21T12:34:00+00:00',
        'timestamp': timestamp,
        'bytes': b'\x00\xffmigration',
        'point': point,
        'reference': FirestoreReferenceValue('Mixed-Case/doc-1'),
        'nan': float('nan'),
        'positive_infinity': float('inf'),
        'negative_infinity': float('-inf'),
    }

    decoded = decode_value(encode_value(value))

    assert decoded['iso_text'] == value['iso_text']
    assert isinstance(decoded['iso_text'], str)
    assert decoded['timestamp'] == timestamp
    assert decoded['bytes'] == value['bytes']
    assert decoded['point'].latitude == point.latitude
    assert decoded['point'].longitude == point.longitude
    assert decoded['reference'].path == 'Mixed-Case/doc-1'
    assert math.isnan(decoded['nan'])
    assert decoded['positive_infinity'] == float('inf')
    assert decoded['negative_infinity'] == float('-inf')
    with pytest.raises(TypeError, match='map keys must be strings'):
        encode_value({1: 'would-collide-with-string-one', '1': 'one'})
    with pytest.raises(TypeError, match='reserved'):
        encode_value({'nested': {'__bad__': 1}})


def test_tagged_codec_preserves_every_double_and_nul_string_or_map_key_without_jsonb_collisions():
    value = {
        'negative_zero': -0.0,
        'large': 1e20,
        'huge': 1e100,
        'nul_string': 'left\x00right',
        'nul_map': {'key\x00part': 'value\x00part'},
    }

    encoded = encode_value(value)
    serialized = json.dumps(encoded, sort_keys=True)
    decoded = decode_value(encoded)

    assert '\\u0000' not in serialized
    assert isinstance(decoded['large'], float) and decoded['large'] == 1e20
    assert isinstance(decoded['huge'], float) and decoded['huge'] == 1e100
    assert decoded['negative_zero'] == 0.0
    assert math.copysign(1.0, decoded['negative_zero']) == -1.0
    assert decoded['nul_string'] == 'left\x00right'
    assert decoded['nul_map'] == {'key\x00part': 'value\x00part'}

    alternate_nan_payload = struct.unpack('>d', bytes.fromhex('7ff8000000000001'))[0]
    assert encode_value(alternate_nan_payload) == encode_value(float('nan'))


def test_root_document_marker_fields_fail_closed_as_firestore_reserved_names():
    document = {'__firestore_pg_value__': 'timestamp', 'value': 'literal-user-data'}

    with pytest.raises(TypeError, match='reserved'):
        encode_document(document)


def test_geo_point_components_preserve_negative_zero_through_json_storage():
    encoded = encode_value(GeoPoint(-0.0, -0.0))
    decoded = decode_value(json.loads(json.dumps(encoded)))

    assert math.copysign(1.0, decoded.latitude) == -1.0
    assert math.copysign(1.0, decoded.longitude) == -1.0


def test_timestamp_codec_is_utc_fixed_nine_digits_at_firestore_microsecond_precision():
    micros = datetime(2026, 8, 21, 20, 34, 0, 123456, tzinfo=timezone.utc)
    nanos = DatetimeWithNanoseconds(2026, 8, 21, 20, 34, tzinfo=timezone.utc, nanosecond=123456789)

    encoded_micros = encode_value(micros)
    encoded_nanos = encode_value(nanos)
    assert encoded_micros['value'] == '2026-08-21T20:34:00.123456000Z'
    assert encoded_nanos['value'] == '2026-08-21T20:34:00.123456000Z'
    assert decode_value(encoded_nanos).microsecond == 123456

    before_epoch = DatetimeWithNanoseconds(
        1969,
        12,
        31,
        23,
        59,
        59,
        tzinfo=timezone.utc,
        nanosecond=123456789,
    )
    # This deliberately matches google-api-core's timestamp_pb(): its
    # negative fractional seconds are converted toward zero before the server
    # truncates nanos to micros.
    assert encode_value(before_epoch)['value'] == '1970-01-01T00:00:00.123456000Z'
    assert encode_value(before_epoch, preserve_timestamp_calendar=True)['value'] == '1969-12-31T23:59:59.123456000Z'
    ordinary_before_epoch = datetime(1969, 12, 31, 23, 59, 59, 123456, tzinfo=timezone.utc)
    assert encode_value(ordinary_before_epoch)['value'] == '1969-12-31T23:59:59.123456000Z'

    stored_document = encode_document({'timestamp': ordinary_before_epoch})
    decoded_for_rmw = decode_stored_document(stored_document)
    decoded_for_rmw['unrelated'] = True
    assert encode_document(decoded_for_rmw)['timestamp'] == stored_document['timestamp']


def test_new_declared_known_collection_requires_new_schema_version(monkeypatch):
    declared = set(migrations.known_collections()) | {'future_upstream_known'}
    monkeypatch.setattr(migrations, '_declared_known_collections', lambda: declared)

    assert migrations.collection_table_name('future_upstream_known').startswith('f_')
    with pytest.raises(migrations.SchemaNotCurrent, match='new firestore_pg schema migration/version'):
        migrations.known_collections()


def test_legacy_populated_tables_fail_closed_before_schema_admission():
    class _Result:
        def __init__(self, present):
            self.present = present

        def fetchone(self):
            return (1,) if self.present else None

    class _Connection:
        def execute(self, statement):
            return _Result('populated' in str(statement))

    assert _legacy_tables_with_rows(_Connection(), {'empty', 'populated'}) == ('populated',)


def test_legacy_table_probe_rejects_unsafe_identifier_before_sql():
    class _Connection:
        def execute(self, _statement):
            raise AssertionError('unsafe identifier reached SQL execution')

    with pytest.raises(SchemaNotCurrent, match='unsafe legacy PostgreSQL table identifier'):
        _legacy_tables_with_rows(_Connection(), {'unsafe-table'})


def test_external_migration_targets_use_structured_host_safety():
    local = {
        'FIRESTORE_PG_DSN': 'postgresql+psycopg://omi:p%40ss@127.0.0.1:5432/omi',
        'AUTH_MIGRATION_DATABASE_URL': 'postgresql://omi:pw@host.docker.internal:5432/auth',
        'FIRESTORE_EMULATOR_HOST': '[::1]:8080',
    }
    assert [target.host for target in validate_external_targets(local)] == [
        '127.0.0.1',
        'host.docker.internal',
        '::1',
    ]

    deceptive = dict(local, FIRESTORE_PG_DSN='postgresql://omi:pw@localhost.evil.invalid:5432/omi')
    with pytest.raises(UnsafeMigrationTarget, match='not loopback/local'):
        validate_external_targets(deceptive)

    remote = dict(
        deceptive,
        ALLOW_REMOTE_MIGRATION_TEST_TARGET='I_ACKNOWLEDGE_THIS_IS_DISPOSABLE',
        AUTH_MIGRATION_DATABASE_URL='postgresql://omi:pw@db.test.invalid:5432/auth',
        FIRESTORE_EMULATOR_HOST='emulator.test.invalid:8080',
    )
    assert len(validate_external_targets(remote)) == 3


def test_sqlalchemy_libpq_query_host_and_port_override_visible_url_authority():
    url = 'postgresql+psycopg://omi:pw@localhost:5432/omi?host=prod.invalid&port=6543&dbname=other'
    engine = create_engine(url)
    try:
        _args, kwargs = engine.dialect.create_connect_args(engine.url)
    finally:
        engine.dispose()

    assert kwargs['host'] == 'prod.invalid'
    assert kwargs['port'] == '6543'
    assert kwargs['dbname'] == 'other'


@pytest.mark.parametrize('env_key', ['FIRESTORE_PG_DSN', 'AUTH_MIGRATION_DATABASE_URL'])
@pytest.mark.parametrize(
    'query_parameter',
    [
        'host=prod.invalid',
        'hostaddr=203.0.113.10',
        'port=6543',
        'dbname=other',
        'options=-csearch_path%3Dprivate',
        'service=prod',
        'servicefile=%2Fsecure%2Fpg_service.conf',
        'sslpassword=secret',
    ],
)
def test_external_migration_targets_reject_libpq_query_target_overrides(env_key, query_parameter):
    local = {
        'FIRESTORE_PG_DSN': 'postgresql+psycopg://omi:pw@127.0.0.1:5432/omi',
        'AUTH_MIGRATION_DATABASE_URL': 'postgresql+psycopg://omi:pw@host.docker.internal:5432/auth',
        'FIRESTORE_EMULATOR_HOST': '127.0.0.1:8080',
    }
    local[env_key] = f'postgresql+psycopg://omi:pw@localhost:5432/test?{query_parameter}'

    with pytest.raises(UnsafeMigrationTarget, match='unsupported libpq query parameters'):
        validate_external_targets(local)


def test_external_migration_targets_allow_audited_tls_query_parameters():
    local = {
        'FIRESTORE_PG_DSN': 'postgresql+psycopg://omi:pw@127.0.0.1:5432/omi?sslmode=require&connect_timeout=5',
        'AUTH_MIGRATION_DATABASE_URL': (
            'postgresql+psycopg://omi:pw@host.docker.internal:5432/auth?sslmode=disable&application_name=cutover'
        ),
        'FIRESTORE_EMULATOR_HOST': '127.0.0.1:8080',
    }

    assert len(validate_external_targets(local)) == 3


def test_engine_dsn_log_label_never_contains_credentials_or_query_secrets():
    dsn = 'postgresql+psycopg://omi:authority-secret@localhost:5432/omi?sslpassword=query-secret&host=prod'

    label = _dsn_host(dsn)

    assert label == 'localhost:5432/omi'
    assert 'secret' not in label


@pytest.mark.parametrize('authority', ['127.0.0.1:8080?host=remote.invalid', '127.0.0.1:8080#remote'])
def test_external_migration_target_rejects_emulator_query_or_fragment(authority):
    env = {
        'FIRESTORE_PG_DSN': 'postgresql+psycopg://omi:pw@127.0.0.1:5432/omi',
        'AUTH_MIGRATION_DATABASE_URL': 'postgresql+psycopg://omi:pw@127.0.0.1:5432/auth',
        'FIRESTORE_EMULATOR_HOST': authority,
    }

    with pytest.raises(UnsafeMigrationTarget, match=r'host\[:port\] authority'):
        validate_external_targets(env)
