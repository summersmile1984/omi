"""Public D1 reads versus the unchanged upstream canonical visibility policy."""

import ast
import __future__
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import typing
import sys
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

import memory_kernel_item as model
from memory_apply_item import read_item
from memory_kernel_short_term_lifecycle import evaluate_short_term_lifecycle
from test_memory_mutation_lock import target

ROOT = Path(__file__).resolve().parents[5]


def original_visibility():
    # Execute the upstream policy functions with the same staged domain types.
    # Storage/network imports and telemetry are controlled; policy bodies are
    # unchanged and independent of the production SQL predicate under test.
    namespace = {
        **vars(typing),
        **vars(model),
        'datetime': datetime,
        'timezone': timezone,
        'dataclass': dataclass,
        'field': field,
        'logging': logging,
        'evaluate_short_term_lifecycle': evaluate_short_term_lifecycle,
        'record_fallback': lambda **kwargs: None,
    }
    for name in [
        'backend/database/product_memory_items.py',
        'backend/utils/memory/canonical_visibility_filter.py',
        'backend/utils/memory/device_scope_filter.py',
        'backend/utils/memory/canonical_memory_adapter.py',
    ]:
        tree = ast.parse((ROOT / name).read_text())
        if name.endswith('canonical_memory_adapter.py'):
            tree.body = [
                n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_canonical_scan_item_visible'
            ]
        else:
            tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        exec(compile(tree, name, 'exec', flags=__future__.annotations.compiler_flag), namespace)
    return namespace['_canonical_scan_item_visible']


def test_http_lists_and_search_filter_lifecycle_before_pagination(target):
    database, request, create = target
    oracle = original_visibility()
    now = datetime.now(timezone.utc)
    cases = [
        ('pending', {}, {}),
        ('pending-sensitive', {'sensitivity_labels_json': '["secret"]'}, {}),
        ('pending-expired', {'captured_at': 1, 'expires_at': 100}, {}),
        ('pending-without-required', {}, {'required': False}),
        ('processed', {'processing_state': 'processed'}, {}),
        ('processed-expired', {'processing_state': 'processed', 'captured_at': 1, 'expires_at': 100}, {}),
        ('processed-sensitive', {'processing_state': 'processed', 'sensitivity_labels_json': '["health"]'}, {}),
        ('processed-hidden', {'processing_state': 'processed', 'status': 'hidden'}, {}),
        ('processed-superseded', {'processing_state': 'processed', 'status': 'superseded'}, {}),
        ('processed-archive', {'processing_state': 'processed', 'memory_tier': 'archive'}, {}),
        ('processed-long-term', {'processing_state': 'processed', 'memory_tier': 'long_term'}, {}),
        ('processed-source-removed', {'processing_state': 'processed', 'source_state': 'tombstoned'}, {}),
        ('blocked', {'processing_state': 'blocked'}, {}),
        ('pending-hidden', {'status': 'hidden'}, {}),
        ('pending-source-removed', {'source_state': 'purged'}, {}),
        ('pending-rejected', {}, {'user_review': False}),
        ('processed-rejected', {'processing_state': 'processed'}, {'user_review': False}),
    ]
    eligible = {True: [], False: []}
    ids = {}
    for index, (name, columns, promotion) in enumerate(cases):
        memory_id = create(content='Lifecycle ' + name)
        ids[name] = memory_id
        row = database.row(memory_id)
        metadata = json.loads(row['canonical_metadata_json'])
        metadata['promotion'].update(promotion)
        updates = {
            'updated_at': int(now.timestamp()) + index,
            **columns,
            'canonical_metadata_json': json.dumps(metadata),
        }
        database.connection.execute(
            'UPDATE cf_memories SET ' + ', '.join(key + ' = ?' for key in updates) + ' WHERE id = ?',
            (*updates.values(), memory_id),
        )
        item = read_item(database.row(memory_id))
        for include_pending in eligible:
            if oracle(
                item,
                policy=model.MemoryAccessPolicy.for_omi_chat(),
                now=now,
                include_pending_processing=include_pending,
                include_archive=False,
                device_scope='all',
                client_device_id=None,
            ):
                eligible[include_pending].insert(0, memory_id)
    assert ids['pending'] in eligible[True] and ids['pending'] not in eligible[False]
    assert ids['processed-expired'] in eligible[False]
    assert ids['processed-archive'] not in eligible[True]
    for path, include_pending in [('/v3/memories', True), ('/memory/search?query=Lifecycle', False)]:
        response = request('GET', path)
        assert response.status_code == 200, response.text
        payload = response.json()
        items = payload if include_pending else payload['items']
        assert [row['memory_id'] for row in items] == eligible[include_pending]
        if not include_pending:
            assert payload['total_count'] == len(eligible[False])
            assert all(row['processing_state'] == 'processed' and row['lifecycle_status'] == 'active' for row in items)
        for offset, expected in enumerate(eligible[include_pending]):
            separator = '&' if '?' in path else '?'
            page = request('GET', path + separator + f'limit=1&offset={offset}').json()
            rows = page if include_pending else page['items']
            assert [row['memory_id'] for row in rows] == [expected]
            if not include_pending:
                assert page['total_count'] == len(eligible[False])
        other = request('GET', path, uid='different-owner').json()
        assert (other if include_pending else other['items']) == []


def test_unmigrated_processed_rows_remain_readable_without_control_or_receipts(target):
    database, request, _ = target
    # Actual pre-journal physical shape: generation zero, empty metadata and no
    # memory control/operation/commit. Reading it must not create those records.
    for name, tier, review in [
        ('historical', 'long_term', None),
        ('archive', 'archive', None),
        ('rejected', 'long_term', 0),
    ]:
        database.connection.execute(
            'INSERT INTO cf_memories (uid,id,content,category,memory_tier,valid_at,created_at,updated_at,user_review) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            ('owner', name, 'Historical ' + name, 'manual', tier, 1, 1, 1, review),
        )
    for path in ['/v3/memories', '/memory/search?query=Historical']:
        result = request('GET', path)
        assert result.status_code == 200, result.text
        payload = result.json()
        rows = payload if isinstance(payload, list) else payload['items']
        assert [row['memory_id'] for row in rows] == ['historical']
    for table in ['cf_memory_apply_control', 'cf_memory_operations', 'cf_memory_commits']:
        assert database.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0


def test_default_read_storage_failure_is_503(target):
    database, request, _ = target
    database.fail = True
    for path in ['/v3/memories', '/memory/search']:
        response = request('GET', path)
        assert response.status_code == 503
        assert 'private storage detail' not in response.text
