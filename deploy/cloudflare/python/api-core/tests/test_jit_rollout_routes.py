"""Client-visible JIT policy from the real D1 owner and registered Core route."""

import json

from test_frame_request_metadata import target

PATH = '/v1/jit/rollout-decision'


def test_decision_uses_owned_current_flags_and_original_wire_contract(target):
    target.db.connection.execute('DELETE FROM cf_jit_flags')
    response = target.call('GET', PATH + '?uid=other&rollout=enabled&kill_switch=disabled')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == {
        'rollout': 'disabled',
        'kill_switch': 'disabled',
        'effective': 'disabled',
        'reason': 'flag_absent',
        'error_class': 'absent',
        'cache_hit': False,
        'cache_ttl_seconds': 0,
    }
    target.db.connection.execute("INSERT INTO cf_jit_flags VALUES ('owner',1,0,1)")
    assert target.call('GET', PATH).json()['effective'] == 'enabled'
    assert target.call('GET', PATH, uid='other').json()['effective'] == 'disabled'
    target.db.connection.execute("INSERT INTO cf_jit_flags VALUES ('',1,1,1)")
    killed = target.call('GET', PATH).json()
    assert killed['effective'] == 'disabled' and killed['reason'] == 'kill_switch_enabled'
    target.db.connection.execute("UPDATE cf_jit_flags SET kill_switch=0 WHERE uid=''")
    assert target.call('GET', PATH).json()['effective'] == 'enabled'


def test_provider_failure_is_unknown_and_contains_no_provider_details(target, capsys):
    def broken(sql):
        raise RuntimeError('private-provider-detail')

    target.db.prepare = broken
    response = target.call('GET', PATH)
    assert response.status_code == 200
    assert response.json() == {
        'rollout': 'unknown',
        'kill_switch': 'unknown',
        'effective': 'unknown',
        'reason': 'provider_error',
        'error_class': 'provider',
        'cache_hit': False,
        'cache_ttl_seconds': 0,
    }
    output = capsys.readouterr().out
    assert 'private-provider-detail' not in output + response.text
    assert json.loads(output)['outcome'] == 'exhausted'


def test_decision_requires_request_bound_identity_and_live_account(target):
    assert target.call('GET', PATH, uid=None).status_code == 401
    assert target.call('GET', PATH, headers={'x-omi-uid': 'owner'}).status_code == 401
    target.db.connection.execute(
        "INSERT INTO cf_account_deletion_tombstones(uid,completed_at,expires_at) VALUES ('owner',1,9999999999)"
    )
    assert target.call('GET', PATH).status_code == 404
    assert target.call('GET', PATH, uid='other').status_code == 200
