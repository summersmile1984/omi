#!/usr/bin/env python3
"""One-time operator tool: reset a Cloudflare stage to an empty first-release state.

Why this exists. The beta stage is stuck between two release contracts:

  * `apply --continue-from` requires every **already published** Worker to be
    byte-identical in the new candidate ("continuation must preserve every already
    published artifact", `release-continuation.mjs`), and
  * a fresh first release requires genuine Worker absence and empty D1
    authorities (`qualifyFirstRelease`: "retained Worker requires executable
    prior-version compatibility evidence", "first deployment requires observed
    empty business schemas and migration ledgers").

The interrupted first release published nine Workers and ran both migration
authorities, and the change that must ship next -- the Web Worker readiness route
that CD's own readiness contract requires -- modifies one of those Workers.
`deploy/cloudflare/release.md` states the limitation plainly: "This narrow
operation does not implement arbitrary upgrade or rollback compatibility."

So the operator resets the stage. This is deliberately NOT part of the release
transaction: it deletes remote state that the transaction treats as owned, and it
is not a capability the pipeline should ever grow on its own. It prints the whole
plan first, refuses to act without an exact confirmation token, and re-reads every
authority to prove it ended up empty.

It talks to the Cloudflare REST API directly rather than through `wrangler`.
`wrangler delete` first lists the account's KV namespaces, so a token scoped for
deployment (Workers Scripts:Edit, D1:Edit, Queues:Edit) fails every deletion with
"Authentication error [code: 10000]" while the CD's own deploy path, which uses
these same REST endpoints (`release-wrangler.mjs`), succeeds.

Delete this file once the stage is released.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = 'https://api.cloudflare.com/client/v4'

# Cloudflare keeps these in every D1 database, and the first-release qualifier
# ignores them too (`SCHEMA_QUERY` in contracts/qualify-prior-schema.mjs).
INTERNAL_OBJECTS = ('_cf_KV',)
OBJECT_TYPES = ('trigger', 'view', 'index', 'table')
OBJECT_KEYWORD = {'trigger': 'TRIGGER', 'view': 'VIEW', 'index': 'INDEX', 'table': 'TABLE'}

# `release-wrangler.mjs` observes a Worker through this endpoint and reads a 404
# carrying error 10007 as "absent"; the reset must agree with the release owner
# about what absent means, so it uses the same pair.
WORKER_NOT_FOUND = 10007
PASSES = 4


class Failure(Exception):
    """A remote call failed in a way the operator has to see."""

    def __init__(self, message: str, codes: tuple = ()):
        super().__init__(message)
        self.codes = tuple(codes)


def confirmation_token(brand: str, stage: str) -> str:
    return f'reset:{brand}:{stage}'


class Api:
    """The whole remote surface: one account, one token, uniform failures."""

    def __init__(self, token: str, account: str, *, request=None):
        if not token:
            raise Failure('CLOUDFLARE_API_TOKEN is unavailable')
        self.token = token
        self.account = account
        self._request = request or http_request

    def call(self, method: str, path: str, body=None):
        payload, status = self._request(method, f'{API_BASE}/accounts/{self.account}{path}', body, self.token)
        if isinstance(payload, dict) and payload.get('success') is True:
            return payload.get('result')
        errors = payload.get('errors') if isinstance(payload, dict) else None
        codes = tuple(error.get('code') for error in errors or [] if isinstance(error, dict))
        detail = '; '.join(
            f"{error.get('code')}: {error.get('message')}" for error in errors or [] if isinstance(error, dict)
        ) or f'HTTP {status}'
        raise Failure(f'{method} {path} failed: {detail}', codes)


def http_request(method: str, url: str, body, token: str):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b'{}'), response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return json.loads(raw or b'{}'), error.code
        except ValueError:
            return {}, error.code


def worker_versions(api: Api, name: str):
    """None when the Worker is absent, otherwise the observed deployments payload."""
    try:
        return api.call('GET', f'/workers/scripts/{name}/deployments')
    except Failure as error:
        if WORKER_NOT_FOUND in error.codes:
            return None
        raise


def delete_worker(api: Api, name: str) -> str:
    """Delete one Worker, tolerating an already-absent one so a retry is safe."""
    if worker_versions(api, name) is None:
        return f'{name} was already absent'
    api.call('DELETE', f'/workers/scripts/{name}?force=true')
    if worker_versions(api, name) is not None:
        raise Failure(f'{name} is still published after the delete')
    return f'deleted {name}'


def queue_consumers(api: Api, queue_id: str) -> list[dict]:
    result = api.call('GET', f'/queues/{queue_id}/consumers')
    return [row for row in result or [] if isinstance(row, dict)]


def remove_consumers(api: Api, workers: list[str]) -> list[str]:
    """Detach the target Workers from every queue.

    Cloudflare refuses to delete a Worker that consumes a queue ("Cannot delete
    this Worker as it is a consumer for a Queue", code 10064), and the release
    owner never removes consumers itself -- `release.md` leaves "queue-consumer
    removal" to separate ownership. That ownership is this reset.
    """
    removed = []
    queues = api.call('GET', '/queues?per_page=100')
    for queue in queues or []:
        queue_id = queue.get('queue_id') if isinstance(queue, dict) else None
        if not queue_id:
            continue
        for consumer in queue_consumers(api, queue_id):
            script = consumer.get('script') or consumer.get('script_name')
            consumer_id = consumer.get('consumer_id')
            if script in workers and consumer_id:
                api.call('DELETE', f'/queues/{queue_id}/consumers/{consumer_id}')
                removed.append(f'{script} -> {queue.get("queue_name") or queue_id}')
    return removed


def objects_sql() -> str:
    names = ', '.join(f"'{name}'" for name in INTERNAL_OBJECTS)
    ordering = ' '.join(f"WHEN '{kind}' THEN {index}" for index, kind in enumerate(OBJECT_TYPES))
    return (
        'SELECT type,name FROM sqlite_master '
        f"WHERE name NOT GLOB 'sqlite_*' AND name NOT IN ({names}) "
        f"AND type IN ({', '.join(repr(kind) for kind in OBJECT_TYPES)}) "
        f'ORDER BY CASE type {ordering} ELSE {len(OBJECT_TYPES)} END, name'
    )


def query(api: Api, database: str, sql: str) -> list[dict]:
    """Run one SQL string and return the rows of its first result set."""
    results = api.call('POST', f'/d1/database/{database}/query', {'sql': sql})
    if not isinstance(results, list) or not results:
        raise Failure(f'the query on {database} returned no result')
    first = results[0]
    if not isinstance(first, dict) or first.get('success') is not True:
        raise Failure(f'the query on {database} failed')
    return first.get('results') or []


def list_objects(api: Api, database: str) -> list[dict]:
    return [row for row in query(api, database, objects_sql()) if isinstance(row, dict)]


def drop_statements(rows: list[dict]) -> list[str]:
    """Drop order is dependency order: triggers reference tables, indexes and
    views depend on tables, and a table must outlive the objects that name it.

    The batch also defers foreign-key enforcement to its commit, because dropping
    a parent table performs an implicit delete of its rows and immediate
    enforcement would abort the drop while a child table still holds references.
    """
    statements = ['PRAGMA defer_foreign_keys = true']
    for kind in OBJECT_TYPES:
        for row in rows:
            name = row.get('name')
            if (row.get('type') or '').lower() != kind or not name or name in INTERNAL_OBJECTS:
                continue
            statements.append(f'DROP {OBJECT_KEYWORD[kind]} IF EXISTS "{name}"')
    return statements


def run(api: Api, database: str, statements: list[str]) -> None:
    query(api, database, ';\n'.join(statements))


def empty_database(api: Api, database: str) -> tuple[int, list[str]]:
    """Drop every user object and prove the authority is empty.

    A single batch is the normal path. If it fails, each statement is retried
    alone (a statement whose dependency is already gone is not a failure), the
    authority is re-read, and up to `PASSES` attempts are made; a pass that
    changes nothing stops the loop and reports what survived.
    """
    dropped, failures, seen = 0, [], []
    for _ in range(PASSES):
        rows = list_objects(api, database)
        if not rows:
            break
        statements = drop_statements(rows)
        try:
            run(api, database, statements)
            failures = []
        except Failure as error:
            failures = []
            for statement in statements:
                try:
                    run(api, database, [statement])
                except Failure as statement_error:
                    text = str(statement_error).lower()
                    if 'no such table' in text or 'no such view' in text or 'no such index' in text:
                        continue
                    failures.append(f'{statement}: {statement_error}')
        after = list_objects(api, database)
        dropped += len(rows) - len(after)
        if not after:
            return dropped, []
        seen = [str(row.get('name')) for row in after]
        if len(after) >= len(rows):
            break
    return dropped, [*seen, *failures]


def parse_databases(entries: list[str]) -> list[tuple[str, str]]:
    databases = []
    for entry in entries:
        name, separator, identifier = entry.partition('=')
        if not separator or not name or not identifier:
            raise Failure(f'--d1 needs NAME=ID, got {entry!r}')
        databases.append((name, identifier))
    return databases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--brand', required=True)
    parser.add_argument('--stage', required=True, choices=['beta', 'production'])
    parser.add_argument('--account', required=True, help='Cloudflare account id that owns the stage')
    parser.add_argument('--worker', action='append', default=[], required=True, help='Worker name to delete; repeatable')
    parser.add_argument('--d1', action='append', default=[], required=True, metavar='NAME=ID', help='D1 authority and id; repeatable')
    parser.add_argument('--confirm', default='', help='exact token required with --apply')
    parser.add_argument('--apply', action='store_true', help='perform the reset (default prints the plan)')
    args = parser.parse_args()

    try:
        databases = parse_databases(args.d1)
    except Failure as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 2

    print(f'Cloudflare stage reset plan: brand={args.brand} stage={args.stage} account={args.account}')
    print(f'  delete {len(args.worker)} Worker(s) (and detach them from every queue):')
    for name in args.worker:
        print(f'    - {name}')
    print(f'  empty {len(databases)} D1 authorit(ies) (drop every trigger, view, index and table):')
    for name, identifier in databases:
        print(f'    - {name} ({identifier})')

    if not args.apply:
        print('\nPlan only. Re-run with --apply and the confirmation token to execute.')
        return 0

    expected = confirmation_token(args.brand, args.stage)
    if args.confirm != expected:
        print(f'ERROR: --confirm must be exactly {expected!r}', file=sys.stderr)
        return 1

    failures = []
    try:
        api = Api(os.environ.get('CLOUDFLARE_API_TOKEN', ''), args.account)
        detached = remove_consumers(api, args.worker)
    except Failure as error:
        print(f'FAILED to read the stage: {error}', file=sys.stderr)
        return 1
    for entry in detached:
        print(f'detached queue consumer {entry}')

    for name in args.worker:
        try:
            print(delete_worker(api, name))
        except Failure as error:
            failures.append(f'{name}: {error}')
            print(f'FAILED to delete {name}: {error}', file=sys.stderr)

    for name, identifier in databases:
        try:
            dropped, remaining = empty_database(api, identifier)
            print(f'emptied {name} ({identifier}): dropped {dropped}, remaining {len(remaining)}')
            if remaining:
                failures.append(f'{name}: still present after the drop: {", ".join(remaining[:20])}')
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
