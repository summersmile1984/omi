#!/usr/bin/env python3
"""Behavioral tests for the one-time Cloudflare stage reset.

The tool deletes remote state, so the tests pin the parts that decide whether it
acts at all and exactly which remote calls it issues; every call is stubbed.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'reset_cloudflare_stage.py'


_MODULE = None


def load_module():
    """One module instance per process: several tests assert on its Failure."""
    global _MODULE
    if _MODULE is None:
        spec = importlib.util.spec_from_file_location('reset_cloudflare_stage', SCRIPT)
        _MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MODULE)
    return _MODULE


def ok(result):
    return {'success': True, 'result': result}, 200


def not_found(code=10007, message='workers script not found'):
    return {'success': False, 'errors': [{'code': code, 'message': message}]}, 404


def denied(code=10000, message='Authentication error'):
    return {'success': False, 'errors': [{'code': code, 'message': message}]}, 403


class FakeCloud:
    """A scripted Cloudflare account: Workers, queues/consumers and D1 catalogs.

    It is strict on purpose. Dropping a table while a trigger that references it
    is still present fails the way the live API does, so the reset's dependency
    order is exercised rather than assumed.
    """

    def __init__(self, *, workers=(), queues=None, catalogues=None, delete_workers=True,
                 zones=('smartipproxy.com',), records=(), deleted_records=True):
        self.workers = set(workers)
        self.queues = {key: dict(value) for key, value in (queues or {}).items()}
        self.catalogues = {key: [dict(row) for row in value] for key, value in (catalogues or {}).items()}
        self.delete_workers = delete_workers
        self.zones = list(zones)
        # hostname -> DNS record type, the zone's public records.
        self.records = {row['name']: row.get('type', 'A') for row in records}
        # Deleting a Worker also deletes the custom domain that created its record.
        self.deleted_records = deleted_records
        self.calls = []

    # -- request seam ------------------------------------------------------
    def __call__(self, method, url, body, token):
        self.calls.append((method, url, body))
        if '/accounts/acct' not in url:
            path = url.split('/client/v4', 1)[1]
        else:
            path = url.split('/client/v4/accounts/acct', 1)[1]
        if path.startswith('/zones?'):
            name = path.split('name=', 1)[1].split('&', 1)[0]
            return ok([{'id': 'zone1', 'name': zone} for zone in self.zones if zone == name])
        if path.startswith('/zones/zone1/dns_records'):
            return self.dns_request(method, path, body)
        if path.startswith('/workers/scripts/'):
            return self.workers_request(method, path)
        if path.startswith('/queues'):
            return self.queues_request(method, path)
        if path.startswith('/d1/database/'):
            return self.d1_request(path, body)
        raise AssertionError(f'unexpected request: {method} {path}')

    def dns_request(self, method, path, body):
        if method == 'GET':
            name = path.split('name=', 1)[1].split('&', 1)[0]
            return ok([{'id': f'id-{host}', 'name': host, 'type': kind, 'proxied': True}
                       for host, kind in sorted(self.records.items()) if host == name])
        if method == 'POST':
            if path != '/zones/zone1/dns_records':
                raise AssertionError(f'unexpected dns path: {path}')
            self.records[body['name']] = body['type']
            self.created = getattr(self, 'created', []) + [body]
            return ok({'id': f"id-{body['name']}", 'name': body['name'], 'type': body['type']})
        raise AssertionError(f'unexpected dns method: {method}')

    def workers_request(self, method, path):
        name = path[len('/workers/scripts/'):].split('?', 1)[0]
        if name.endswith('/deployments'):
            name = name[: -len('/deployments')]
        if method == 'GET':
            if name in self.workers:
                return ok({'deployments': [{'id': 'v1'}]})
            return not_found()
        if method == 'DELETE':
            if name not in self.workers:
                return not_found()
            if self.consumed_by(name):
                return {'success': False, 'errors': [{'code': 10064, 'message': 'Cannot delete this Worker as it is a consumer for a Queue.'}]}, 400
            if self.delete_workers:
                self.workers.discard(name)
            return ok({'id': name})
        raise AssertionError(f'unexpected worker method: {method}')

    def consumed_by(self, name):
        return any(
            consumer.get('script') == name
            for queue in self.queues.values()
            for consumer in queue.get('consumers', [])
        )

    def queues_request(self, method, path):
        if path.startswith('/queues?'):
            return ok([{'queue_id': key, 'queue_name': value.get('queue_name', key)} for key, value in self.queues.items()])
        parts = path.split('/')
        queue_id, consumers = parts[2], self.queues.get(parts[2], {}).get('consumers', [])
        if len(parts) == 4 and parts[3] == 'consumers' and method == 'GET':
            return ok(consumers)
        if len(parts) == 5 and parts[3] == 'consumers' and method == 'DELETE':
            self.queues[queue_id]['consumers'] = [
                consumer for consumer in consumers if consumer.get('consumer_id') != parts[4]
            ]
            return ok({'consumer_id': parts[4]})
        raise AssertionError(f'unexpected queue request: {method} {path}')

    def d1_request(self, path, body):
        database = path.split('/')[3]
        sql = (body or {}).get('sql', '')
        rows = self.catalogues.setdefault(database, [])
        if sql.lstrip().upper().startswith('SELECT'):
            return ok([{'results': rows, 'success': True}])
        try:
            self.apply(sql, rows)
        except ApiError as error:
            return {'success': False, 'errors': [{'code': 7500, 'message': str(error)}]}, 400
        return ok([{'results': [], 'success': True}])

    def apply(self, sql, rows):
        for statement in sql.split(';\n'):
            _, _, remainder = statement.partition(' IF EXISTS ')
            if not remainder:
                continue
            name = remainder.strip().strip('"')
            for row in list(rows):
                if row['name'] != name:
                    continue
                if row['type'] == 'table':
                    blocker = next(
                        (other for other in rows
                         if other['type'] == 'trigger' and row['name'] in (other.get('sql') or '')),
                        None,
                    )
                    if blocker:
                        raise ApiError(f'no such table: main.{row["name"]}: SQLITE_ERROR')
                rows.remove(row)


class ApiError(Exception):
    """Raised by the fake to mimic a statement-level D1 failure."""


class DropStatementsTests(unittest.TestCase):
    def test_drops_are_ordered_so_dependencies_outlive_their_dependents(self):
        statements = load_module().drop_statements([
            {'type': 'table', 'name': 'cf_account_deletion_intents'},
            {'type': 'view', 'name': 'recent'},
            {'type': 'index', 'name': 'idx'},
            {'type': 'trigger', 'name': 'guarded'},
        ])
        self.assertEqual(statements[0], 'PRAGMA defer_foreign_keys = true')
        self.assertEqual(
            statements[1:],
            [
                'DROP TRIGGER IF EXISTS "guarded"',
                'DROP VIEW IF EXISTS "recent"',
                'DROP INDEX IF EXISTS "idx"',
                'DROP TABLE IF EXISTS "cf_account_deletion_intents"',
            ],
        )

    def test_internal_objects_are_never_listed_or_dropped(self):
        module = load_module()
        self.assertIn("NOT GLOB 'sqlite_*'", module.objects_sql())
        self.assertIn("'_cf_KV'", module.objects_sql())
        self.assertNotIn('DROP', module.objects_sql())
        self.assertEqual(module.drop_statements([{'type': 'table', 'name': '_cf_KV'}]), ['PRAGMA defer_foreign_keys = true'])

    def test_every_user_object_type_is_dropped(self):
        module = load_module()
        statements = module.drop_statements([{'type': kind, 'name': f'{kind}_one'} for kind in module.OBJECT_TYPES])
        for kind in module.OBJECT_TYPES:
            self.assertIn(f'DROP {module.OBJECT_KEYWORD[kind]} IF EXISTS "{kind}_one"', statements)


class EmptyDatabaseTests(unittest.TestCase):
    def empty(self, fake, database='app'):
        module = load_module()
        return module.empty_database(module.Api('token', 'acct', request=fake), database)

    def test_a_trigger_referencing_a_dropped_table_does_not_abort_the_batch(self):
        # The live app authority failed exactly this way: the trigger bodies name
        # cf_account_deletion_intents, so dropping that table first left every
        # trigger unparsable and the whole batch aborted.
        fake = FakeCloud(catalogues={'app': [
            {'type': 'table', 'name': 'cf_account_deletion_intents', 'sql': 'CREATE TABLE'},
            {'type': 'trigger', 'name': 'cf_conversations_guard', 'sql': 'SELECT 1 FROM cf_account_deletion_intents'},
            {'type': 'view', 'name': 'cf_effective_user_subscriptions'},
            {'type': 'index', 'name': 'cf_conversations_uid'},
        ]})
        module = load_module()
        # The fake only fails when a trigger outlives its table, and apply() is
        # never reached because drop_statements already orders triggers first.
        dropped, remaining = module.empty_database(module.Api('token', 'acct', request=fake), 'app')
        self.assertEqual(remaining, [])
        self.assertEqual(dropped, 4)
        self.assertEqual(fake.catalogues['app'], [])

    def test_the_batch_is_one_request_and_is_verified_by_a_re_read(self):
        fake = FakeCloud(catalogues={'app': [{'type': 'table', 'name': 'd1_migrations'}]})
        self.empty(fake)
        writes = [call for call in fake.calls if call[2] and 'DROP TABLE' in call[2]['sql']]
        self.assertEqual(len(writes), 1)
        self.assertIn('DROP TABLE IF EXISTS "d1_migrations"', writes[0][2]['sql'])
        self.assertEqual(fake.catalogues['app'], [])

    def test_an_object_that_survives_the_drop_is_reported(self):
        fake = FakeCloud(catalogues={'app': [{'type': 'table', 'name': 'stuck'}]})
        fake.apply = lambda sql, rows: None  # pretends to drop, changes nothing
        _dropped, remaining = self.empty(fake)
        self.assertEqual(remaining, ['stuck'])


class WorkerTests(unittest.TestCase):
    def call(self, fake, name, **kwargs):
        module = load_module()
        return module.delete_worker(module.Api('token', 'acct', request=fake), name, **kwargs)

    def test_a_worker_is_deleted_through_the_rest_endpoint_and_verified_absent(self):
        fake = FakeCloud(workers=['eddy-cf-auth-beta'])
        self.assertEqual(self.call(fake, 'eddy-cf-auth-beta'), 'deleted eddy-cf-auth-beta')
        methods = [(method, url.split('/accounts/acct', 1)[1]) for method, url, _ in fake.calls]
        self.assertEqual(methods, [
            ('GET', '/workers/scripts/eddy-cf-auth-beta/deployments'),
            ('DELETE', '/workers/scripts/eddy-cf-auth-beta?force=true'),
            ('GET', '/workers/scripts/eddy-cf-auth-beta/deployments'),
        ])

    def test_an_already_absent_worker_is_tolerated(self):
        self.assertIn('already absent', self.call(FakeCloud(), 'eddy-web-beta'))

    def test_a_worker_that_survives_the_delete_is_a_failure(self):
        fake = FakeCloud(workers=['eddy-web-beta'], delete_workers=False)
        module = load_module()
        with self.assertRaises(module.Failure):
            self.call(fake, 'eddy-web-beta')

    def test_a_denied_observation_is_not_read_as_absence(self):
        fake = FakeCloud(workers=['eddy-web-beta'])
        fake.workers_request = lambda method, path: denied()
        module = load_module()
        with self.assertRaises(module.Failure):
            self.call(fake, 'eddy-web-beta')

    def test_queue_consumers_are_detached_before_the_worker_is_deleted(self):
        fake = FakeCloud(
            workers=['eddy-cf-jobs-beta'],
            queues={'q1': {'queue_name': 'eddy-cf-jobs-beta', 'consumers': [
                {'consumer_id': 'c1', 'script': 'eddy-cf-jobs-beta'},
                {'consumer_id': 'c2', 'script': 'someone-else'},
            ]}},
        )
        module = load_module()
        api = module.Api('token', 'acct', request=fake)
        self.assertEqual(module.remove_consumers(api, ['eddy-cf-jobs-beta']), ['eddy-cf-jobs-beta -> eddy-cf-jobs-beta'])
        # The foreign consumer is left alone, and the worker can now be deleted.
        self.assertEqual([row['consumer_id'] for row in fake.queues['q1']['consumers']], ['c2'])
        self.assertEqual(module.delete_worker(api, 'eddy-cf-jobs-beta'), 'deleted eddy-cf-jobs-beta')


class DnsTests(unittest.TestCase):
    def test_a_missing_hostname_gets_the_documented_originless_placeholder(self):
        fake = FakeCloud()
        module = load_module()
        api = module.Api('token', 'acct', request=fake)
        zone = module.zone_identifier(api, 'smartipproxy.com')
        self.assertEqual(zone, 'zone1')
        self.assertEqual(
            module.ensure_record(api, zone, 'eddy-cf-beta.smartipproxy.com'),
            'eddy-cf-beta.smartipproxy.com -> proxied A 192.0.2.0',
        )
        record = fake.created[0]
        self.assertEqual(
            (record['type'], record['content'], record['proxied'], record['ttl']),
            ('A', '192.0.2.0', True, 1),
        )
        self.assertEqual(module.unresolvable(api, zone, ['eddy-cf-beta.smartipproxy.com']), [])

    def test_an_existing_record_is_reported_and_left_alone(self):
        fake = FakeCloud(records=[{'name': 'eddy-cf-beta.smartipproxy.com', 'type': 'CNAME'}])
        module = load_module()
        api = module.Api('token', 'acct', request=fake)
        self.assertEqual(
            module.ensure_record(api, 'zone1', 'eddy-cf-beta.smartipproxy.com'),
            'eddy-cf-beta.smartipproxy.com already has a CNAME record',
        )
        self.assertEqual([method for method, _, _ in fake.calls], ['GET'])

    def test_a_zone_that_is_not_uniquely_owned_fails_closed(self):
        fake = FakeCloud(zones=('smartipproxy.com', 'smartipproxy.com'))
        module = load_module()
        with self.assertRaises(module.Failure):
            module.zone_identifier(module.Api('token', 'acct', request=fake), 'smartipproxy.com')

    def test_a_hostname_that_stays_unresolvable_is_reported(self):
        fake = FakeCloud()
        module = load_module()
        api = module.Api('token', 'acct', request=fake)
        self.assertEqual(module.unresolvable(api, 'zone1', ['eddy-cf-beta.smartipproxy.com']),
                         ['eddy-cf-beta.smartipproxy.com'])


class MainTests(unittest.TestCase):
    def invoke(self, module, argv, fake, token='token'):
        module.http_request = fake
        original, sys.argv = sys.argv, ['reset_cloudflare_stage.py', *argv]
        previous = os.environ.get('CLOUDFLARE_API_TOKEN')
        os.environ['CLOUDFLARE_API_TOKEN'] = token
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = module.main()
        finally:
            sys.argv = original
            if previous is None:
                os.environ.pop('CLOUDFLARE_API_TOKEN', None)
            else:
                os.environ['CLOUDFLARE_API_TOKEN'] = previous
        return code, out.getvalue(), err.getvalue()

    def base_argv(self):
        return [
            '--brand', 'eddy', '--stage', 'beta', '--account', 'acct', '--zone', 'smartipproxy.com',
            '--worker', 'eddy-cf-auth-beta', '--worker', 'eddy-web-beta',
            '--hostname', 'eddy-cf-beta.smartipproxy.com',
            '--hostname', 'eddy-cf-beta-auth.smartipproxy.com',
            '--d1', 'app=42bbfb33', '--d1', 'auth=56f2ed9e',
        ]

    def test_plan_mode_touches_nothing(self):
        module = load_module()
        fake = FakeCloud()
        code, out, _ = self.invoke(module, self.base_argv(), fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [])
        self.assertIn('delete 2 Worker(s)', out)
        self.assertIn('ensure 2 hostname(s)', out)
        self.assertIn('empty 2 D1 authorit(ies)', out)
        self.assertIn('Plan only', out)

    def test_apply_restores_the_hostname_dns_records_and_verifies_them(self):
        module = load_module()
        fake = FakeCloud(workers=['eddy-cf-auth-beta', 'eddy-web-beta'])
        code, out, err = self.invoke(module, [*self.base_argv(), '--apply', '--confirm', 'reset:eddy:beta'], fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            sorted((row['name'], row['content']) for row in fake.created),
            [('eddy-cf-beta-auth.smartipproxy.com', '192.0.2.0'),
             ('eddy-cf-beta.smartipproxy.com', '192.0.2.0')],
        )
        self.assertIn('dns eddy-cf-beta.smartipproxy.com -> proxied A 192.0.2.0', out)
        self.assertIn('Reset complete', out)

    def test_a_hostname_left_unresolvable_fails_the_reset(self):
        module = load_module()
        fake = FakeCloud()
        fake.dns_request = lambda method, path, body: ok([]) if method == 'GET' else ok({})
        code, _, err = self.invoke(module, [*self.base_argv(), '--apply', '--confirm', 'reset:eddy:beta'], fake)
        self.assertEqual(code, 1)
        self.assertIn('still has no record', err)

    def test_a_hostname_outside_the_zone_fails_closed(self):
        module = load_module()
        fake = FakeCloud()
        argv = [*self.base_argv(), '--hostname', 'eddy-cf-beta.example.com']
        code, _, err = self.invoke(module, argv, fake)
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])
        self.assertIn('outside --zone', err)

    def test_apply_requires_the_exact_confirmation_token(self):
        module = load_module()
        fake = FakeCloud()
        code, _, err = self.invoke(module, [*self.base_argv(), '--apply', '--confirm', 'reset:eddy:production'], fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, [])
        self.assertIn("--confirm must be exactly 'reset:eddy:beta'", err)

    def test_apply_empties_the_stage_and_verifies_it(self):
        module = load_module()
        fake = FakeCloud(
            workers=['eddy-cf-auth-beta', 'eddy-web-beta'],
            catalogues={
                '42bbfb33': [{'type': 'table', 'name': 'd1_migrations'}],
                '56f2ed9e': [{'type': 'table', 'name': 'users'}],
            },
        )
        code, out, err = self.invoke(module, [*self.base_argv(), '--apply', '--confirm', 'reset:eddy:beta'], fake)
        self.assertEqual(code, 0, err)
        deleted = [url.split('/workers/scripts/', 1)[1] for method, url, _ in fake.calls if method == 'DELETE']
        self.assertEqual(deleted, ['eddy-cf-auth-beta?force=true', 'eddy-web-beta?force=true'])
        self.assertEqual(fake.workers, set())
        self.assertIn('remaining 0', out)
        self.assertIn('Reset complete', out)
        self.assertEqual(fake.catalogues, {'42bbfb33': [], '56f2ed9e': []})

    def test_a_failed_deletion_is_reported_and_exits_nonzero(self):
        module = load_module()
        fake = FakeCloud(workers=['eddy-cf-auth-beta', 'eddy-web-beta'], delete_workers=False)
        code, _, err = self.invoke(module, [*self.base_argv(), '--apply', '--confirm', 'reset:eddy:beta'], fake)
        self.assertEqual(code, 1)
        self.assertIn('RESET INCOMPLETE', err)

    def test_a_missing_token_is_refused_before_any_call(self):
        module = load_module()
        fake = FakeCloud()
        code, _, err = self.invoke(module, [*self.base_argv(), '--apply', '--confirm', 'reset:eddy:beta'], fake, token='')
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, [])
        self.assertIn('CLOUDFLARE_API_TOKEN', err)

    def test_a_malformed_authority_fails_closed(self):
        module = load_module()
        fake = FakeCloud()
        argv = ['--brand', 'eddy', '--stage', 'beta', '--account', 'acct', '--zone', 'smartipproxy.com',
                '--worker', 'w', '--hostname', 'w.smartipproxy.com', '--d1', 'app42bbfb33']
        code, _, err = self.invoke(module, argv, fake)
        self.assertEqual(code, 2)
        self.assertIn('NAME=VALUE', err)


if __name__ == '__main__':
    unittest.main(verbosity=2)
