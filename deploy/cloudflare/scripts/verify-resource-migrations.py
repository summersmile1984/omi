#!/usr/bin/env python3
"""Execute a resource plan's exact SQL authorities in isolated SQLite fixtures.

This checks SQL ordering, identity-preserving additive upgrades and plan reentry.
It does not execute Wrangler, roll back D1, or certify older Worker versions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


def verify(root: Path, plan: dict, *, sql_root: Path | None = None, include_schema: bool = False) -> list[dict]:
    authorities = plan.get('migrations', [])
    if sorted(entry.get('authority', '') for entry in authorities) != ['app', 'auth']:
        raise ValueError('exactly one Auth and App migration authority is required')
    results = []
    for authority in authorities:
        name = authority['authority']
        directory = (sql_root if sql_root is not None else root / 'migrations') / name
        if authority.get('directory') != f'migrations/{name}':
            raise ValueError('migration directory does not belong to its authority')
        expected = sorted(path.name for path in directory.glob('*.sql'))
        files = authority.get('files', [])
        if [entry.get('name') for entry in files] != expected or not expected:
            raise ValueError('migration plan is missing, reordering or duplicating a SQL file')
        sources = []
        for entry in files:
            content = (directory / entry['name']).read_bytes()
            if hashlib.sha256(content).hexdigest() != entry.get('sha256'):
                raise ValueError('migration content differs from the planned digest')
            sources.append(content.decode('utf-8'))
        db = sqlite3.connect(':memory:')
        applied: dict[str, str] = {}

        def apply(entries: list[dict], sql: list[str]) -> int:
            count = 0
            for entry, content in zip(entries, sql):
                if entry['name'] in applied:
                    if applied[entry['name']] != entry['sha256']:
                        raise ValueError('an applied migration changed')
                    continue
                db.executescript(content)
                applied[entry['name']] = entry['sha256']
                count += 1
            return count

        # The last migration must retain a principal/task readable by the
        # preceding schema. Neither fixture has new provenance/state metadata.
        apply(files[:-1], sources[:-1])
        if name == 'auth':
            db.execute(
                'INSERT INTO user (id,name,email,createdAt,updatedAt) VALUES (?,?,?,?,?)',
                ('legacy-fixture', 'Fixture', 'legacy@fixture.invalid', 1, 1),
            )
            db.execute(
                'INSERT INTO session (id,expiresAt,token,createdAt,updatedAt,userId) VALUES (?,?,?,?,?,?)',
                ('fixture-session', 2_000_000_000, 'synthetic-session-value', 1, 1, 'legacy-fixture'),
            )
            read = "SELECT user.id, session.userId FROM user JOIN session ON session.userId=user.id WHERE user.id='legacy-fixture'"
        else:
            db.execute(
                'INSERT INTO cf_action_items (uid,id,description,status,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                ('legacy-fixture', 'fixture-task', 'Synthetic task', 'active', 1, 1),
            )
            read = (
                "SELECT uid,id,status,completed FROM cf_action_items WHERE uid='legacy-fixture' AND id='fixture-task'"
            )
        before = db.execute(read).fetchall()
        if len(before) != 1:
            raise ValueError('legacy fixture is missing before migration')
        db.commit()
        apply(files[-1:], sources[-1:])
        if db.execute(read).fetchall() != before:
            raise ValueError('last migration changed the existing principal/task')
        if apply(files, sources) != 0:
            raise ValueError('same migration plan was not idempotent')
        result = {'authority': name, 'sql_files': len(files), 'legacy_row_preserved': True, 'reentry_applied': 0}
        if include_schema:
            result['schema_catalog'] = [
                dict(zip(('type', 'name', 'tbl_name', 'sql'), row))
                for row in db.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
                )
            ]
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('frozen SQL fixture contains invalid foreign keys')
        results.append(result)
        db.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--sql-root', type=Path, help='Explicit frozen SQL directory containing auth/ and app/')
    parser.add_argument('--include-schema', action='store_true', help='Return the schema actually executed by SQLite')
    args = parser.parse_args()
    try:
        result = verify(args.root, json.load(sys.stdin), sql_root=args.sql_root, include_schema=args.include_schema)
        print(json.dumps({'sql_fixture': result, 'older_worker_compatibility_proven': False}))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
