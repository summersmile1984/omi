"""Live PostgreSQL + Firestore emulator migration/import contract."""

from __future__ import annotations

import os
import math
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

# Keep the real SDK module reference before importing firestore_pg migrations;
# database package initialization installs the shim when FIRESTORE_PG_DSN is set.
from google.cloud import firestore as cloud_firestore
from google.cloud.firestore_v1 import FieldFilter as CloudFieldFilter
from google.cloud.firestore_v1 import GeoPoint
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.api_core import exceptions as api_exceptions

cloud_firestore = sys.modules.get('google.cloud.firestore._real', cloud_firestore)

from firestore_pg import ArrayRemove, ArrayUnion, FieldFilter, Increment  # noqa: E402
from firestore_pg.client import Client, UnsupportedFirestoreQuery, transactional as pg_transactional  # noqa: E402
from firestore_pg.importer import run_import, target_inventory  # noqa: E402
from firestore_pg.migrations import check_schema, migrate, provision_collections  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get('FIRESTORE_PG_DSN') or not os.environ.get('FIRESTORE_EMULATOR_HOST'),
    reason='needs live PostgreSQL and Firestore emulator',
)


def test_forward_migration_imports_full_paths_and_reconciles(tmp_path):
    first = migrate()
    second = migrate()
    assert first.current_version == second.current_version == 1
    assert check_schema().latest_version == 1

    source = cloud_firestore.Client(project=os.environ.get('FIREBASE_PROJECT_ID', 'demo-omi-local'))
    root = source.document('pg_import_root/present')
    missing_parent = source.document('pg_import_root/missing')
    nested = source.document('pg_import_root/missing/pg_import_nested/child')
    mixed = source.document('PG-Import-Mixed/doc-1')
    mixed_second = source.document('PG-Import-Mixed/doc-2')
    order_a = source.document('pg_import_order/a')
    order_nested = source.document('pg_import_order/a/pg_import_order_nested/x')
    order_bang = source.document('pg_import_order/a!')
    timestamp = DatetimeWithNanoseconds(2026, 8, 21, 12, 34, tzinfo=timezone.utc, nanosecond=123456789)
    same_microsecond = DatetimeWithNanoseconds(2026, 8, 21, 12, 35, tzinfo=timezone.utc, nanosecond=123456000)
    later_nanosecond = DatetimeWithNanoseconds(2026, 8, 21, 12, 35, tzinfo=timezone.utc, nanosecond=123456789)
    before_epoch_sdk_nanos = DatetimeWithNanoseconds(
        1969,
        12,
        31,
        23,
        59,
        59,
        tzinfo=timezone.utc,
        nanosecond=123456789,
    )
    before_epoch_datetime = datetime(1969, 12, 31, 23, 59, 59, 123456, tzinfo=timezone.utc)
    time_before_epoch_datetime = source.document('pg_import_timestamps/pre-epoch-datetime')
    time_before_epoch_sdk_nanos = source.document('pg_import_timestamps/pre-epoch-sdk-nanos')
    time_low = source.document('pg_import_timestamps/a')
    time_high = source.document('pg_import_timestamps/b')
    numeric_documents = {
        'neg-double': {'v': -0.0, 'arr': [-0.0], 's': 'A', 'mixed': None, 'neq': None},
        'pos-double': {'v': 0.0, 'arr': [0.0], 's': 'a', 'mixed': False, 'neq': float('inf')},
        'one-double': {'v': 1.0, 'arr': [1.0], 's': 'é', 'mixed': 1, 'neq': float('-inf')},
        'one-int': {'v': 1, 'arr': [1], 's': '中', 'mixed': timestamp, 'neq': float('nan')},
        'large-20': {'v': 1e20, 'mixed': 'a', 'neq': 'x'},
        'large-100': {'v': 1e100},
    }
    numeric_refs = {doc_id: source.document(f'pg_import_numeric/{doc_id}') for doc_id in numeric_documents}
    root.set(
        {
            'marker': 'root',
            'number': 1,
            'iso_text': '2026-08-21T12:34:00+00:00',
            'timestamp': timestamp,
            'bytes': b'\x00\xffmigration',
            'point': GeoPoint(31.2304, 121.4737),
            'negative_point': GeoPoint(-0.0, -0.0),
            'reference': nested,
            'nan': float('nan'),
            'positive_infinity': float('inf'),
            'negative_infinity': float('-inf'),
            'negative_zero': -0.0,
            'large_double': 1e20,
            'huge_double': 1e100,
            'nul_string': 'left\x00right',
            'nul_map': {'key\x00part': 'value\x00part'},
            'nul_mixed_map': {'normal': 1, 'bad\x00key': 'x'},
        }
    )
    nested.set({'marker': 'nested', 'number': 2})
    mixed.set({'marker': 'mixed-case', 'number': 3})
    mixed_second.set({'marker': 'mixed-case-second', 'number': 4})
    order_a.set({'marker': 'order-a'})
    order_nested.set({'marker': 'order-nested'})
    order_bang.set({'marker': 'order-bang'})
    time_before_epoch_datetime.set({'timestamp': before_epoch_datetime})
    time_before_epoch_sdk_nanos.set({'timestamp': before_epoch_sdk_nanos})
    time_low.set({'timestamp': same_microsecond})
    time_high.set({'timestamp': later_nanosecond})
    for doc_id, payload in numeric_documents.items():
        numeric_refs[doc_id].set(payload)
    assert not missing_parent.get().exists
    assert nested.get().exists
    checkpoint = tmp_path / 'firestore-import-checkpoint.json'

    result = run_import(source, checkpoint, checkpoint_interval=1)

    assert result['status'] == 'passed'
    assert result['source_count'] == result['target_count'] == 17
    assert result['source_content_hash'] == result['target_content_hash']
    target = Client(project='pg-import-target')
    with pytest.raises(api_exceptions.InvalidArgument, match='reserved'):
        source.document('pg_import_root/reserved-nested-source').set({'map': {'__bad__': 1}})
    with pytest.raises(TypeError, match='reserved'):
        target.document('pg_import_root/reserved-nested-target').set({'map': {'__bad__': 1}})
    imported_root = target.document(root.path).get().to_dict()
    assert imported_root['marker'] == 'root'
    assert imported_root['iso_text'] == '2026-08-21T12:34:00+00:00'
    assert isinstance(imported_root['iso_text'], str)
    assert imported_root['timestamp'] == timestamp
    assert imported_root['timestamp'].nanosecond == 123456000
    assert imported_root['bytes'] == b'\x00\xffmigration'
    assert imported_root['point'].latitude == 31.2304
    assert imported_root['point'].longitude == 121.4737
    assert math.copysign(1.0, imported_root['negative_point'].latitude) == -1.0
    assert math.copysign(1.0, imported_root['negative_point'].longitude) == -1.0
    assert imported_root['reference'].path == nested.path
    assert imported_root['reference'].get().to_dict() == {'marker': 'nested', 'number': 2}
    assert math.isnan(imported_root['nan'])
    assert imported_root['positive_infinity'] == float('inf')
    assert imported_root['negative_infinity'] == float('-inf')
    assert isinstance(imported_root['large_double'], float) and imported_root['large_double'] == 1e20
    assert isinstance(imported_root['huge_double'], float) and imported_root['huge_double'] == 1e100
    assert math.copysign(1.0, imported_root['negative_zero']) == -1.0
    assert imported_root['nul_string'] == 'left\x00right'
    assert imported_root['nul_map'] == {'key\x00part': 'value\x00part'}
    assert imported_root['nul_mixed_map'] == {'normal': 1, 'bad\x00key': 'x'}
    assert [
        snapshot.id
        for snapshot in source.collection('pg_import_root')
        .where(filter=CloudFieldFilter('nul_mixed_map.normal', '==', 1))
        .stream()
    ] == ['present']
    with pytest.raises(UnsupportedFirestoreQuery, match='ancestor map contains a NUL key'):
        list(target.collection('pg_import_root').where(filter=FieldFilter('nul_mixed_map.normal', '==', 1)).stream())
    target.document(root.path).update({'counter': Increment(1), 'labels': ArrayUnion(['roundtrip'])})
    updated_root = target.document(root.path).get().to_dict()
    assert updated_root['counter'] == 1
    assert updated_root['labels'] == ['roundtrip']
    assert updated_root['timestamp'].nanosecond == 123456000
    assert updated_root['bytes'] == b'\x00\xffmigration'
    assert updated_root['reference'].get().to_dict() == {'marker': 'nested', 'number': 2}
    assert math.isnan(updated_root['nan'])
    imported_pre_epoch = target.document(time_before_epoch_datetime.path)
    original_pre_epoch = imported_pre_epoch.get().to_dict()['timestamp']
    imported_pre_epoch.update({'unrelated': True})
    assert imported_pre_epoch.get().to_dict()['timestamp'] == original_pre_epoch == before_epoch_datetime

    source.document(root.path).update({'transform_values': cloud_firestore.ArrayUnion([float('nan'), 1, {'n': 1}])})
    target.document(root.path).update({'transform_values': ArrayUnion([float('nan'), 1, {'n': 1}])})
    source.document(root.path).update(
        {'transform_values': cloud_firestore.ArrayUnion([float('nan'), 1.0, {'n': 1.0}, 2])}
    )
    target.document(root.path).update({'transform_values': ArrayUnion([float('nan'), 1.0, {'n': 1.0}, 2])})
    assert len(source.document(root.path).get().to_dict()['transform_values']) == 4
    assert len(target.document(root.path).get().to_dict()['transform_values']) == 4
    source.document(root.path).update(
        {'transform_values': cloud_firestore.ArrayRemove([float('nan'), 1.0, {'n': 1.0}])}
    )
    target.document(root.path).update({'transform_values': ArrayRemove([float('nan'), 1.0, {'n': 1.0}])})
    assert source.document(root.path).get().to_dict()['transform_values'] == [2]
    assert target.document(root.path).get().to_dict()['transform_values'] == [2]
    nan_matches = target.collection('pg_import_root').where(filter=FieldFilter('nan', '==', float('nan'))).stream()
    assert [snapshot.id for snapshot in nan_matches] == ['present']
    assert [
        snapshot.id
        for snapshot in target.collection('pg_import_root')
        .where(filter=FieldFilter('bytes', '==', b'\x00\xffmigration'))
        .stream()
    ] == ['present']
    assert [
        snapshot.id
        for snapshot in target.collection('pg_import_root')
        .where(filter=FieldFilter('point', '==', GeoPoint(31.2304, 121.4737)))
        .stream()
    ] == ['present']
    assert [
        snapshot.id
        for snapshot in target.collection('pg_import_root')
        .where(filter=FieldFilter('reference', '==', nested))
        .stream()
    ] == ['present']
    with pytest.raises(UnsupportedFirestoreQuery):
        list(target.collection('pg_import_root').order_by('bytes').stream())
    with pytest.raises(UnsupportedFirestoreQuery):
        list(target.collection('pg_import_root').where(filter=FieldFilter('point', '>', GeoPoint(0, 0))).stream())
    with pytest.raises(UnsupportedFirestoreQuery):
        list(target.collection('pg_import_root').order_by('reference').start_after({'reference': nested}).stream())
    with pytest.raises(UnsupportedFirestoreQuery):
        list(target.collection('pg_import_root').order_by('nan').stream())

    source_numeric = source.collection('pg_import_numeric')
    target_numeric = target.collection('pg_import_numeric')
    for query_value in (1, 1.0):
        source_ids = sorted(
            snapshot.id for snapshot in source_numeric.where(filter=CloudFieldFilter('v', '==', query_value)).stream()
        )
        target_ids = sorted(
            snapshot.id for snapshot in target_numeric.where(filter=FieldFilter('v', '==', query_value)).stream()
        )
        assert source_ids == target_ids == ['one-double', 'one-int']
        source_in_ids = sorted(
            snapshot.id for snapshot in source_numeric.where(filter=CloudFieldFilter('v', 'in', [query_value])).stream()
        )
        target_in_ids = sorted(
            snapshot.id for snapshot in target_numeric.where(filter=FieldFilter('v', 'in', [query_value])).stream()
        )
        assert source_in_ids == target_in_ids == ['one-double', 'one-int']
        source_array_ids = sorted(
            snapshot.id
            for snapshot in source_numeric.where(filter=CloudFieldFilter('arr', 'array_contains', query_value)).stream()
        )
        target_array_ids = sorted(
            snapshot.id
            for snapshot in target_numeric.where(filter=FieldFilter('arr', 'array-contains', query_value)).stream()
        )
        assert source_array_ids == target_array_ids == ['one-double', 'one-int']
        assert sorted(
            snapshot.id
            for snapshot in source_numeric.where(
                filter=CloudFieldFilter('arr', 'array_contains_any', [query_value])
            ).stream()
        ) == ['one-double', 'one-int']
        assert sorted(
            snapshot.id
            for snapshot in target_numeric.where(
                filter=FieldFilter('arr', 'array-contains-any', [query_value])
            ).stream()
        ) == ['one-double', 'one-int']
    for query_value in (-0.0, 0.0):
        assert sorted(
            snapshot.id for snapshot in source_numeric.where(filter=CloudFieldFilter('v', '==', query_value)).stream()
        ) == ['neg-double', 'pos-double']
        assert sorted(
            snapshot.id for snapshot in target_numeric.where(filter=FieldFilter('v', '==', query_value)).stream()
        ) == ['neg-double', 'pos-double']

    expected_neq = ['large-20', 'one-double', 'one-int', 'pos-double']
    for operator, query_value in (('!=', 1), ('not-in', [1])):
        assert (
            sorted(
                snapshot.id
                for snapshot in source_numeric.where(filter=CloudFieldFilter('neq', operator, query_value)).stream()
            )
            == expected_neq
        )
        assert (
            sorted(
                snapshot.id
                for snapshot in target_numeric.where(filter=FieldFilter('neq', operator, query_value)).stream()
            )
            == expected_neq
        )

    nan_membership_expectations = {
        '==': ['one-int'],
        '!=': ['large-20', 'one-double', 'pos-double'],
        'in': [],
        'not-in': expected_neq,
    }
    for operator, expected in nan_membership_expectations.items():
        query_value = float('nan') if operator in ('==', '!=') else [float('nan')]
        assert (
            sorted(
                snapshot.id
                for snapshot in source_numeric.where(filter=CloudFieldFilter('neq', operator, query_value)).stream()
            )
            == expected
        )
        assert (
            sorted(
                snapshot.id
                for snapshot in target_numeric.where(filter=FieldFilter('neq', operator, query_value)).stream()
            )
            == expected
        )

    for operator, query_value, expected in (
        ('<', 'm', ['large-20']),
        ('>', 0, ['one-double']),
        ('>=', False, ['pos-double']),
    ):
        assert (
            sorted(
                snapshot.id
                for snapshot in source_numeric.where(filter=CloudFieldFilter('mixed', operator, query_value)).stream()
            )
            == expected
        )
        assert (
            sorted(
                snapshot.id
                for snapshot in target_numeric.where(filter=FieldFilter('mixed', operator, query_value)).stream()
            )
            == expected
        )

    for operator, query_value in (
        ('==', [1]),
        ('in', [[1]]),
        ('not-in', [{'n': 1}]),
        ('array-contains', {'n': 1}),
    ):
        with pytest.raises(UnsupportedFirestoreQuery, match='recursive Firestore numeric equality'):
            list(target_numeric.where(filter=FieldFilter('v', operator, query_value)).stream())

    expected_numeric_order = ['neg-double', 'pos-double', 'one-double', 'one-int', 'large-20', 'large-100']
    source_numeric_ordered = list(source_numeric.order_by('v').stream())
    target_numeric_ordered = list(target_numeric.order_by('v').stream())
    assert [snapshot.id for snapshot in source_numeric_ordered] == expected_numeric_order
    assert [snapshot.id for snapshot in target_numeric_ordered] == expected_numeric_order
    expected_non_one = ['neg-double', 'pos-double', 'large-20', 'large-100']
    for operator in ('!=', 'not-in'):
        source_value = 1 if operator == '!=' else [1]
        target_value = 1.0 if operator == '!=' else [1.0]
        assert [
            snapshot.id
            for snapshot in source_numeric.where(filter=CloudFieldFilter('v', operator, source_value))
            .order_by('v')
            .stream()
        ] == expected_non_one
        assert [
            snapshot.id
            for snapshot in target_numeric.where(filter=FieldFilter('v', operator, target_value)).order_by('v').stream()
        ] == expected_non_one
    expected_greater_than_zero = ['one-double', 'one-int', 'large-20', 'large-100']
    assert [
        snapshot.id for snapshot in source_numeric.where(filter=CloudFieldFilter('v', '>', 0)).order_by('v').stream()
    ] == expected_greater_than_zero
    assert [
        snapshot.id for snapshot in target_numeric.where(filter=FieldFilter('v', '>', 0.0)).order_by('v').stream()
    ] == expected_greater_than_zero
    assert [
        snapshot.id for snapshot in source_numeric.order_by('v').start_after(source_numeric_ordered[2]).stream()
    ] == ['one-int', 'large-20', 'large-100']
    assert [
        snapshot.id for snapshot in target_numeric.order_by('v').start_after(target_numeric_ordered[2]).stream()
    ] == ['one-int', 'large-20', 'large-100']
    expected_string_order = ['neg-double', 'pos-double', 'one-double', 'one-int']
    source_string_ordered = list(source_numeric.order_by('s').stream())
    target_string_ordered = list(target_numeric.order_by('s').stream())
    assert [snapshot.id for snapshot in source_string_ordered] == expected_string_order
    assert [snapshot.id for snapshot in target_string_ordered] == expected_string_order
    assert [
        snapshot.id for snapshot in source_numeric.order_by('s').start_after(source_string_ordered[1]).stream()
    ] == ['one-double', 'one-int']
    assert [
        snapshot.id for snapshot in target_numeric.order_by('s').start_after(target_string_ordered[1]).stream()
    ] == ['one-double', 'one-int']

    source_times = source.collection('pg_import_timestamps')
    target_times = target.collection('pg_import_timestamps')
    source_ordered = list(source_times.order_by('timestamp').stream())
    target_ordered = list(target_times.order_by('timestamp').stream())
    expected_order = [
        ('pre-epoch-datetime', 123456),
        ('pre-epoch-sdk-nanos', 123456),
        ('a', 123456),
        ('b', 123456),
    ]
    assert [(snapshot.id, snapshot.to_dict()['timestamp'].microsecond) for snapshot in source_ordered] == expected_order
    assert [(snapshot.id, snapshot.to_dict()['timestamp'].microsecond) for snapshot in target_ordered] == expected_order
    assert source_ordered[0].to_dict()['timestamp'].year == target_ordered[0].to_dict()['timestamp'].year == 1969
    # google-api-core DatetimeWithNanoseconds.timestamp_pb() converts negative
    # fractional seconds toward zero, so the emulator stores this input in
    # 1970. The shim deliberately matches that SDK wire behavior.
    assert source_ordered[1].to_dict()['timestamp'].year == target_ordered[1].to_dict()['timestamp'].year == 1970
    assert [
        snapshot.id
        for snapshot in source_times.where(filter=CloudFieldFilter('timestamp', '>', before_epoch_sdk_nanos)).stream()
    ] == ['a', 'b']
    assert [
        snapshot.id
        for snapshot in target_times.where(filter=FieldFilter('timestamp', '>', before_epoch_sdk_nanos)).stream()
    ] == ['a', 'b']
    assert [
        snapshot.id
        for snapshot in source_times.where(filter=CloudFieldFilter('timestamp', '>', same_microsecond)).stream()
    ] == []
    assert [
        snapshot.id for snapshot in target_times.where(filter=FieldFilter('timestamp', '>', same_microsecond)).stream()
    ] == []
    source_first = source_ordered[2]
    target_first = target_ordered[2]
    assert [snapshot.id for snapshot in source_times.order_by('timestamp').start_after(source_first).stream()] == ['b']
    assert [snapshot.id for snapshot in target_times.order_by('timestamp').start_after(target_first).stream()] == ['b']
    assert target.document(nested.path).get().to_dict() == {'marker': 'nested', 'number': 2}
    assert target.document(mixed.path).get().to_dict() == {'marker': 'mixed-case', 'number': 3}
    group = target.collection_group('PG-Import-Mixed')
    grouped_paths = [snapshot.reference.path for snapshot in group.order_by('__name__').stream()]
    assert grouped_paths == [mixed.path, mixed_second.path]
    assert [
        snapshot.reference.path for snapshot in group.where(filter=FieldFilter('__name__', '==', mixed)).stream()
    ] == [mixed.path]
    assert [
        snapshot.reference.path
        for snapshot in group.where(filter=FieldFilter('__name__', 'in', [mixed, mixed_second])).stream()
    ] == [mixed.path, mixed_second.path]
    first = target.document(mixed.path).get()
    assert [snapshot.reference.path for snapshot in group.order_by('__name__').start_after(first).stream()] == [
        mixed_second.path
    ]
    assert target_inventory().count == 17

    target.document(time_high.path).delete()
    target.document(time_low.path).delete()
    target.document(time_before_epoch_sdk_nanos.path).delete()
    target.document(time_before_epoch_datetime.path).delete()
    target.document(mixed_second.path).delete()
    target.document(mixed.path).delete()
    target.document(nested.path).delete()
    target.document(root.path).delete()
    target.document(order_nested.path).delete()
    target.document(order_bang.path).delete()
    target.document(order_a.path).delete()
    for ref in numeric_refs.values():
        target.document(ref.path).delete()
    mixed.delete()
    mixed_second.delete()
    nested.delete()
    root.delete()
    order_nested.delete()
    order_bang.delete()
    order_a.delete()
    for ref in numeric_refs.values():
        ref.delete()
    time_high.delete()
    time_low.delete()
    time_before_epoch_sdk_nanos.delete()
    time_before_epoch_datetime.delete()


def _exercise_write_skew(client, transactional_decorator, collection_id):
    collection = client.collection(collection_id)
    refs = [collection.document('a'), collection.document('b')]
    for ref in refs:
        ref.set({'on_call': True})
    barrier = threading.Barrier(2)
    attempts = {'a': 0, 'b': 0}
    attempts_lock = threading.Lock()

    @transactional_decorator
    def leave_if_redundant(tx, own_id):
        with attempts_lock:
            attempts[own_id] += 1
            attempt = attempts[own_id]
        snapshots = [ref.get(transaction=tx) for ref in refs]
        if attempt == 1:
            barrier.wait(timeout=10)
        if all(snapshot.to_dict()['on_call'] for snapshot in snapshots):
            tx.update(collection.document(own_id), {'on_call': False})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(leave_if_redundant, client.transaction(), doc_id) for doc_id in ('a', 'b')]
        for future in futures:
            future.result(timeout=30)

    outcome = [ref.get().to_dict()['on_call'] for ref in refs]
    for ref in refs:
        ref.delete()
    return outcome, attempts


def test_serializable_write_skew_matches_firestore_emulator():
    migrate()
    provision_collections({'pg_write_skew'})
    source = cloud_firestore.Client(project=os.environ.get('FIREBASE_PROJECT_ID', 'demo-omi-local'))
    target = Client(project='pg-write-skew-target')

    source_outcome, source_attempts = _exercise_write_skew(
        source,
        cloud_firestore.transactional,
        'firestore_write_skew',
    )
    target_outcome, target_attempts = _exercise_write_skew(target, pg_transactional, 'pg_write_skew')

    assert sorted(source_outcome) == sorted(target_outcome) == [False, True]
    assert sum(source_attempts.values()) >= 3
    assert sum(target_attempts.values()) >= 3
