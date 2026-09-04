"""Receipt/provider closure fault tests through production wrappers."""

from contextlib import nullcontext
from unittest import mock

import pytest

from fork import provider_guard as owner
from database.legal_holds import DestructiveOperationInProgress


def test_existing_principal_can_write_but_receipt_blocks_every_consumer(monkeypatch):
    from fork.patches.provider_guard import patches, CONSUMERS

    monkeypatch.setattr(owner, 'account_lock', lambda *args, **kwargs: nullcontext())
    state = [None]
    monkeypatch.setattr(owner, 'get_status', lambda *args, **kwargs: state[0])
    for patch in patches():
        if patch.module not in CONSUMERS or patch.attribute != 'external_write_fence':
            continue
        original = mock.Mock(side_effect=lambda *args, **kwargs: nullcontext())
        wrapped = patch.build(original)
        with wrapped('legacy-principal'):
            pass
        state[0] = 'completed'
        with pytest.raises(DestructiveOperationInProgress):
            with wrapped('legacy-principal'):
                pytest.fail('write admitted')
        assert original.call_count == 1
        state[0] = None


def test_completion_proof_failure_preserves_receipt_authority(monkeypatch):
    from fork import auth_identity

    monkeypatch.setattr(owner, 'account_lock', lambda *args, **kwargs: nullcontext())
    identity = mock.Mock()
    monkeypatch.setattr(auth_identity, 'assert_erased', identity)
    complete = mock.Mock(return_value=True)
    monkeypatch.setattr(owner, 'assert_erased', mock.Mock(side_effect=RuntimeError('residual')))
    with pytest.raises(RuntimeError):
        owner.complete(complete)('owner')
    complete.assert_not_called()
    identity.assert_not_called()
    monkeypatch.setattr(owner, 'assert_erased', mock.Mock())
    assert owner.complete(complete)('owner') is True
    identity.assert_called_once_with('owner')


def test_provider_failure_is_required_and_keeps_upstream_failure(monkeypatch):
    result = {
        'required_failures': [{'operation': 'existing', 'error': 'existing'}],
        'best_effort_failures': [],
        'vectors_deleted': 0,
    }
    monkeypatch.setattr(owner, 'external_index', mock.Mock(side_effect=RuntimeError('secret response')))
    value = owner.purge(lambda uid: result)('owner')
    assert [item['operation'] for item in value['required_failures']] == ['existing', 'self_host_provider_closure']
    assert 'secret' not in repr(value)


def test_busy_wipe_never_enters_irreversible_work(monkeypatch):
    from database import users

    monkeypatch.setattr(owner, 'account_lock', mock.Mock(side_effect=owner.ProviderOperationBusy()))
    failed = mock.Mock()
    monkeypatch.setattr(users, 'mark_user_deletion_wipe_failed', failed)
    wipe = mock.Mock()
    assert owner.wipe(wipe)('owner') is False
    wipe.assert_not_called()
    failed.assert_called_once_with('owner')
