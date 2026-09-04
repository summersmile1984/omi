"""Execute the PG write owner with nested SDK values; SQL is the controlled seam."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from unittest import mock

import pytest
from google.cloud.firestore_v1 import transforms as sdk

from firestore_pg import client, write_policy
from firestore_pg.codec import decode_stored_document, encode_document


class Row:
    def __init__(self, value=None):
        self.value = value
        self.rowcount = 1

    def fetchone(self):
        return self.value


class StoredRow:
    def __init__(self, value=None):
        self.value = None if value is None else encode_document(value)
        self.writes = 0

    def execute(self, query, params=None):
        sql = str(query)
        if sql.startswith('SELECT data,'):
            return Row(None if self.value is None else (deepcopy(self.value), datetime.now(timezone.utc), 1))
        if sql.startswith(('INSERT ', 'UPDATE ')):
            self.value = json.loads(params['data'])
            self.writes += 1
            return Row()
        raise AssertionError(f'unexpected SQL at controlled seam: {sql}')

    def read(self):
        return decode_stored_document(self.value)


@pytest.fixture
def write_owner(monkeypatch):
    def bind(previous=None):
        row = StoredRow(previous)
        monkeypatch.setattr(client, 'require_table', lambda _table: None)
        monkeypatch.setattr(client, 'get_engine', mock.Mock())
        monkeypatch.setattr(client, 'get_tx_conn', lambda: row)
        monkeypatch.setattr(write_policy, 'policy', write_policy.UnrestrictedWrites())
        return client.DocumentReference('llm_usage', 'users/synthetic-owner', 'fixture-day'), row

    return bind


@pytest.mark.parametrize('transforms', [sdk, client])
def test_nested_usage_merge_accumulates_and_preserves_other_models(write_owner, transforms):
    ref, row = write_owner(
        {
            'chat': {'model': {'input_tokens': 8, 'output_tokens': 2, 'call_count': 1}, 'other': {'input_tokens': 90}},
            'legacy': True,
        }
    )
    patch = {
        'chat': {
            'model': {
                'input_tokens': transforms.Increment(3),
                'output_tokens': transforms.Increment(2),
                'call_count': transforms.Increment(1),
            }
        },
        'plan_usage': {'_unattributed': {'_metadata': {'cost_status_counts': {'missing': transforms.Increment(1)}}}},
        'date': 'fixture-day',
    }
    ref.set(patch, merge=True)
    ref.set(patch, merge=True)
    value = row.read()
    assert value['chat'] == {
        'model': {'input_tokens': 14, 'output_tokens': 6, 'call_count': 3},
        'other': {'input_tokens': 90},
    }
    assert value['plan_usage']['_unattributed']['_metadata']['cost_status_counts']['missing'] == 2
    assert value['legacy'] is True
    assert row.writes == 2


def test_nested_plain_merge_preserves_siblings_and_empty_map_overwrites(write_owner):
    ref, row = write_owner({'settings': {'name': 'old', 'keep': True}, 'clear': {'old': 1}})
    ref.set({'settings': {'name': 'new'}, 'clear': {}}, merge=True)
    assert row.read() == {'settings': {'name': 'new', 'keep': True}, 'clear': {}}


@pytest.mark.parametrize('operation', ['set', 'create', 'update'])
def test_nested_transforms_are_values_only_after_materialization(write_owner, operation):
    ref, row = write_owner(None if operation == 'create' else {'box': {'old': True, 'count': 99}})
    getattr(ref, operation)({'box': {'count': sdk.Increment(2), 'tags': sdk.ArrayUnion(['a'])}})
    assert row.read() == {'box': {'count': 2, 'tags': ['a']}}


def test_nested_literal_keys_and_multiple_transform_kinds(write_owner):
    ref, row = write_owner({'box': {'literal.dot': 5, 'tags': ['a', 'b'], 'erase': 1}})
    ref.set(
        {
            'box': {
                'literal.dot': sdk.Increment(2),
                'tags': sdk.ArrayRemove(['a']),
                'erase': sdk.DELETE_FIELD,
                'clock': sdk.SERVER_TIMESTAMP,
            }
        },
        merge=True,
    )
    value = row.read()['box']
    assert value['literal.dot'] == 7
    assert value['tags'] == ['b']
    assert 'erase' not in value and 'literal' not in value
    assert isinstance(value['clock'], datetime)


def test_transform_inside_array_is_rejected_before_sql(write_owner):
    ref, row = write_owner()
    with pytest.raises(TypeError, match='unsupported Firestore value'):
        ref.set({'bad': [sdk.Increment(1)]}, merge=True)
    assert row.writes == 0


@pytest.mark.parametrize('operation', ['set', 'create', 'update'])
def test_forbidden_nested_delete_matches_installed_sdk_and_never_writes(write_owner, operation):
    from google.cloud.firestore_v1 import _helpers

    payload = {'box': {'gone': sdk.DELETE_FIELD}}
    document = 'projects/fixture/databases/(default)/documents/users/synthetic-owner'
    with pytest.raises(ValueError):
        if operation == 'create':
            _helpers.pbs_for_create(document, payload)
        elif operation == 'set':
            _helpers.pbs_for_set_no_merge(document, payload)
        else:
            _helpers.pbs_for_update(document, payload, None)
    prior = {'box': {'keep': 7, 'gone': 1}}
    ref, row = write_owner(prior)
    with pytest.raises(ValueError, match='DELETE_FIELD'):
        getattr(ref, operation)(payload)
    assert row.writes == 0
    assert row.read() == prior
