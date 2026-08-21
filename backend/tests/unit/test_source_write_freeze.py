from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import source_write_freeze as freeze

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
SECRET = 'unit-source-freeze-secret'


def _issue(path: Path, *, scopes: list[str] | None = None, ttl_seconds: int = 3600) -> dict:
    return freeze.issue_lease(
        path,
        source_project='source-project',
        source_database='(default)',
        source_endpoint='https://firestore.googleapis.com/',
        scopes=scopes or ['firestore', 'storage'],
        holder='change-123',
        ttl_seconds=ttl_seconds,
        secret=SECRET,
        now=NOW,
    )


def test_issue_and_verify_binds_authority_scopes_and_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / 'freeze.json'
    payload = _issue(path)

    assert payload['status'] == 'active'
    assert path.stat().st_mode & 0o777 == 0o600
    result = freeze.verify_lease(
        path,
        source_project='source-project',
        source_database='(default)',
        source_endpoint='https://firestore.googleapis.com',
        required_scopes={'firestore'},
        secret=SECRET,
        now=NOW + timedelta(seconds=1),
    )

    assert result['status'] == 'passed'
    assert result['lease_id'] == payload['lease_id']
    assert result['scopes'] == ['firestore', 'storage']


@pytest.mark.parametrize(
    ('mutation', 'message'),
    [
        (
            {'source': {'project': 'other', 'database': '(default)', 'endpoint': 'https://firestore.googleapis.com'}},
            'source authority',
        ),
        ({'status': 'revoked'}, 'not an active'),
        ({'expires_at': '2026-08-21T12:00:01Z'}, 'not currently active'),
    ],
)
def test_verify_rejects_tampering_even_when_signature_field_is_retained(
    tmp_path: Path, mutation: dict, message: str
) -> None:
    path = tmp_path / 'freeze.json'
    _issue(path)
    value = json.loads(path.read_text(encoding='utf-8'))
    value.update(mutation)
    path.write_text(json.dumps(value), encoding='utf-8')
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    with pytest.raises(freeze.SourceWriteFreezeError, match=message):
        freeze.verify_lease(
            path,
            source_project='source-project',
            source_database='(default)',
            source_endpoint='https://firestore.googleapis.com',
            required_scopes={'firestore'},
            secret=SECRET,
            now=NOW + timedelta(seconds=2),
        )


def test_verify_rejects_scope_gap_wrong_secret_and_broad_permissions(tmp_path: Path) -> None:
    path = tmp_path / 'freeze.json'
    _issue(path, scopes=['firestore'])

    with pytest.raises(freeze.SourceWriteFreezeError, match='does not cover'):
        freeze.verify_lease(
            path,
            source_project='source-project',
            source_database='(default)',
            source_endpoint='https://firestore.googleapis.com',
            required_scopes={'storage'},
            secret=SECRET,
            now=NOW,
        )
    with pytest.raises(freeze.SourceWriteFreezeError, match='signature'):
        freeze.verify_lease(
            path,
            source_project='source-project',
            source_database='(default)',
            source_endpoint='https://firestore.googleapis.com',
            required_scopes={'firestore'},
            secret='wrong-secret',
            now=NOW,
        )
    os.chmod(path, 0o644)
    with pytest.raises(freeze.SourceWriteFreezeError, match='0600'):
        freeze.verify_lease(
            path,
            source_project='source-project',
            source_database='(default)',
            source_endpoint='https://firestore.googleapis.com',
            required_scopes={'firestore'},
            secret=SECRET,
            now=NOW,
        )


def test_verify_rejects_symlinked_or_non_regular_lease(tmp_path: Path) -> None:
    target = tmp_path / 'freeze-target.json'
    _issue(target)

    link = tmp_path / 'freeze-link.json'
    link.symlink_to(target)
    with pytest.raises(freeze.SourceWriteFreezeError, match='regular file'):
        freeze.verify_lease(
            link,
            source_project='source-project',
            source_database='(default)',
            source_endpoint='https://firestore.googleapis.com',
            required_scopes={'firestore'},
            secret=SECRET,
            now=NOW,
        )

    directory = tmp_path / 'freeze-directory.json'
    directory.mkdir()
    with pytest.raises(freeze.SourceWriteFreezeError, match='regular file'):
        freeze.verify_lease(
            directory,
            source_project='source-project',
            source_database='(default)',
            source_endpoint='https://firestore.googleapis.com',
            required_scopes={'firestore'},
            secret=SECRET,
            now=NOW,
        )


def test_issue_refuses_overwrite_and_excessive_ttl(tmp_path: Path) -> None:
    path = tmp_path / 'freeze.json'
    _issue(path)

    with pytest.raises(freeze.SourceWriteFreezeError, match='already exists'):
        _issue(path)
    with pytest.raises(freeze.SourceWriteFreezeError, match='TTL'):
        _issue(tmp_path / 'too-long.json', ttl_seconds=freeze.MAX_TTL_SECONDS + 1)
