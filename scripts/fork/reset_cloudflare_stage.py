#!/usr/bin/env python3
"""One-time operator tool: reset a Cloudflare stage to an empty first-release state.

Why this exists. The beta stage is stuck between two release contracts:

  * `apply --continue-from` requires every **already published** Worker to be
    byte-identical in the new candidate ("continuation must preserve every already
    published artifact", `release-continuation.mjs`), and
  * a fresh first release requires absent Workers and empty D1 authorities.

The interrupted first release published nine Workers and ran both migration
authorities, and the change that must ship next -- the Web Worker readiness route
that CD's own readiness contract requires -- modifies one of those Workers.
`deploy/cloudflare/release.md` states the limitation plainly: "This narrow
operation does not implement arbitrary upgrade or rollback compatibility."

So the operator resets the stage. This is deliberately NOT part of the release
transaction: it deletes remote state that the transaction treats as owned, and it
is not a capability the pipeline should ever grow on its own. It prints the whole
plan first, refuses to act without an exact confirmation token, and verifies that
each D1 authority ends up empty.

Delete this file once the stage is released.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

# Cloudflare keeps these in every D1 database; the first-release qualifier
# ignores them too (`SCHEMA_QUERY` in contracts/qualify-prior-schema.mjs).
INTERNAL_OBJECTS = ('_cf_KV',)
NOT_FOUND = ('not found', 'does not exist', 'could not find', 'no such worker')


class Failure(Exception):
    """A remote command failed in a way the operator has to see."""


def confirmation_token(brand: str, stage: str) -> str:
    return f'reset:{brand}:{stage}'


def run_wrangler(wrangler: Path, args: list[str], *, run=None) -> str:
    run = run or subprocess.run
    command = [str(wrangler), *args]
    result = run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Failure(f"{shlex.join(command)} failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def delete_worker(wrangler: Path, name: str, *, run=None) -> str:
    """Delete one Worker, tolerating an already-absent one so a retry is safe."""
    try:
        return run_wrangler(wrangler, ['delete', name, '--force'], run=run)
    except Failure as error:
        if any(marker in str(error).lower() for marker in NOT_FOUND):
            return f'{name} was already absent'
        raise


def objects_sql() -> str:
    names = ', '.join(f"'{name}'" for name in INTERNAL_OBJECTS)
    return (
        'SELECT type,name FROM sqlite_master '
        f"WHERE name NOT LIKE 'sqlite_%' AND name NOT IN ({names}) "
        "AND type IN ('table','view') ORDER BY type DESC, name"
    )


def list_objects(wrangler: Path, database: str, *, run=None) -> list[dict]:
    output = run_wrangler(
        wrangler,
        ['d1', 'execute', database, '--remote', '--json', '--yes', '--command', objects_sql()],
        run=run,
    )
    start = output.find('[')
    if start < 0:
        raise Failure(f'could not read the object list for {database}: {output.strip()[:200]}')
    try:
        payload = json.loads(output[start:])
    except ValueError as error:
        raise Failure(f'could not parse the object list for {database}: {error}')
    if not isinstance(payload, list) or not payload:
        return []
    # `wrangler d1 execute --json` returns one entry per statement.
    rows = payload[0].get('results') if isinstance(payload[0], dict) else None
    if rows is None:
        raise Failure(f'the object list for {database} has no results')
    return rows


def drop_statements(rows: list[dict]) -> list[str]:
    statements = []
    for row in rows:
        kind = (row.get('type') or '').lower()
        name = row.get('name')
        if not name or kind not in ('table', 'view') or name in INTERNAL_OBJECTS:
            continue
        keyword = 'TABLE' if kind == 'table' else 'VIEW'
        statements.append(f'DROP {keyword} IF EXISTS "{name}"')
    return statements


def empty_database(wrangler: Path, database: str, *, run=None) -> tuple[int, int]:
    """Drop every user object and confirm the authority is empty. Returns (dropped, remaining)."""
    rows = list_objects(wrangler, database, run=run)
    statements = drop_statements(rows)
    if statements:
        run_wrangler(
            wrangler,
            ['d1', 'execute', database, '--remote', '--yes', '--command', '; '.join(statements)],
            run=run,
        )
    return len(statements), len(list_objects(wrangler, database, run=run))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--brand', required=True)
    parser.add_argument('--stage', required=True, choices=['beta', 'production'])
    parser.add_argument('--worker', action='append', default=[], required=True, help='Worker name to delete; repeatable')
    parser.add_argument('--d1', action='append', default=[], required=True, metavar='NAME=ID', help='D1 authority and id; repeatable')
    parser.add_argument('--wrangler', type=Path, default=Path('deploy/cloudflare/node_modules/.bin/wrangler'))
    parser.add_argument('--confirm', default='', help=f'exact token required with --apply: {confirmation_token("eddy", "beta")}')
    parser.add_argument('--apply', action='store_true', help='perform the reset (default prints the plan)')
    args = parser.parse_args()

    databases = []
    for entry in args.d1:
        if '=' not in entry:
            print(f'ERROR: --d1 needs NAME=ID, got {entry!r}', file=sys.stderr)
            return 2
        name, identifier = entry.split('=', 1)
        databases.append((name, identifier))

    print(f'Cloudflare stage reset plan: brand={args.brand} stage={args.stage}')
    print(f'  delete {len(args.worker)} Worker(s):')
    for name in args.worker:
        print(f'    - {name}')
    print(f'  empty {len(databases)} D1 authorit(ies) (drop every table and view):')
    for name, identifier in databases:
        print(f'    - {name} ({identifier})')

    if not args.apply:
        print('\nPlan only. Re-run with --apply and the confirmation token to execute.')
        return 0

    expected = confirmation_token(args.brand, args.stage)
    if args.confirm != expected:
        print(f'ERROR: --confirm must be exactly {expected!r}', file=sys.stderr)
        return 1
    if not args.wrangler.is_file():
        print(f'ERROR: wrangler not found at {args.wrangler}', file=sys.stderr)
        return 2

    failures = []
    for name in args.worker:
        try:
            delete_worker(args.wrangler, name)
            print(f'deleted {name}')
        except Failure as error:
            failures.append(f'{name}: {error}')
            print(f'FAILED to delete {name}: {error}', file=sys.stderr)

    for name, identifier in databases:
        try:
            dropped, remaining = empty_database(args.wrangler, identifier)
            print(f'emptied {name} ({identifier}): dropped {dropped}, remaining {remaining}')
            if remaining:
                failures.append(f'{name}: {remaining} object(s) still present after the drop')
        except Failure as error:
            failures.append(f'{name}: {error}')
            print(f'FAILED to empty {name}: {error}', file=sys.stderr)

    if failures:
        print('\nRESET INCOMPLETE:', file=sys.stderr)
        for failure in failures:
            print(f'  {failure}', file=sys.stderr)
        return 1
    print('\nReset complete: the stage now has no Workers and empty D1 authorities.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
