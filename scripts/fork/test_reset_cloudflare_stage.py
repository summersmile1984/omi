#!/usr/bin/env python3
"""Behavioral tests for the one-time Cloudflare stage reset.

The tool deletes remote state, so the tests pin the parts that decide whether it
acts at all and exactly which commands it issues; every remote call is stubbed.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'reset_cloudflare_stage.py'


def load_module():
    spec = importlib.util.spec_from_file_location('reset_cloudflare_stage', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Result:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def d1_payload(rows):
    return json.dumps([{'results': rows, 'success': True}]) + '\n'


class FakeWrangler:
    """Answers the object listing from a scripted per-database catalogue."""

    def __init__(self, catalogues):
        # catalogues: {database_id: [rows...]} mutated as drops are executed.
        self.catalogues = {key: list(value) for key, value in catalogues.items()}
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append([str(part) for part in command])
        args = [str(part) for part in command[1:]]
        if args[:1] == ['delete']:
            return Result()
        if args[:2] == ['d1', 'execute']:
            database = args[2]
            sql = args[args.index('--command') + 1]
            rows = list(self.catalogues.get(database, []))
            if sql.startswith('SELECT'):
                return Result(stdout=d1_payload(rows))
            for row in rows:
                if f'DROP TABLE IF EXISTS "{row["name"]}"' in sql or f'DROP VIEW IF EXISTS "{row["name"]}"' in sql:
                    self.catalogues[database].remove(row)
            return Result(stdout='executed\n')
        raise AssertionError(f'unexpected command: {command}')


class DeleteTests(unittest.TestCase):
    def test_workers_are_deleted_with_force(self):
        module = load_module()
        fake = FakeWrangler({})
        module.delete_worker(Path('wrangler'), 'eddy-cf-auth-beta', run=fake)
        self.assertEqual(fake.commands, [['wrangler', 'delete', 'eddy-cf-auth-beta', '--force']])

    def test_an_already_absent_worker_is_tolerated(self):
        module = load_module()

        def absent(command, **kwargs):
            return Result(returncode=1, stderr='Worker "x" was not found')

        self.assertIn('already absent', module.delete_worker(Path('wrangler'), 'x', run=absent))

    def test_a_hard_failure_is_raised(self):
        module = load_module()

        def denied(command, **kwargs):
            return Result(returncode=1, stderr='Authentication error [code: 10000]')

        with self.assertRaises(module.Failure):
            module.delete_worker(Path('wrangler'), 'x', run=denied)


class EmptyDatabaseTests(unittest.TestCase):
    def test_every_table_and_view_is_dropped_and_the_authority_is_reverified(self):
        module = load_module()
        catalogues = {
            'app': [
                {'type': 'table', 'name': 'd1_migrations'},
                {'type': 'table', 'name': 'conversations'},
                {'type': 'view', 'name': 'recent'},
            ]
        }
        fake = FakeWrangler(catalogues)
        dropped, remaining = module.empty_database(Path('wrangler'), 'app', run=fake)
        self.assertEqual((dropped, remaining), (3, 0))
        drop = fake.commands[-2][-1]
        self.assertIn('DROP TABLE IF EXISTS "d1_migrations"', drop)
        self.assertIn('DROP TABLE IF EXISTS "conversations"', drop)
        self.assertIn('DROP VIEW IF EXISTS "recent"', drop)
        # The last call re-reads the catalogue, so the caller sees the real result.
        self.assertTrue(fake.commands[-1][-1].startswith('SELECT'))

    def test_internal_objects_are_never_listed_or_dropped(self):
        module = load_module()
        query = module.objects_sql()
        self.assertIn("name NOT LIKE 'sqlite_%'", query)
        self.assertIn("'_cf_KV'", query)
        self.assertNotIn('DROP', query)
        self.assertEqual(module.drop_statements([{'type': 'table', 'name': '_cf_KV'}]), [])
        self.assertEqual(module.drop_statements([{'type': 'index', 'name': 'idx'}]), [])

    def test_an_object_that_survives_the_drop_is_reported(self):
        module = load_module()

        class Stubborn(FakeWrangler):
            def __call__(self, command, **kwargs):
                args = [str(part) for part in command[1:]]
                if args[:2] == ['d1', 'execute'] and not args[args.index('--command') + 1].startswith('SELECT'):
                    self.commands.append([str(part) for part in command])
                    return Result()  # pretends to drop, changes nothing
                return super().__call__(command, **kwargs)

        fake = Stubborn({'app': [{'type': 'table', 'name': 'stuck'}]})
        dropped, remaining = module.empty_database(Path('wrangler'), 'app', run=fake)
        self.assertEqual((dropped, remaining), (1, 1))


class MainTests(unittest.TestCase):
    def invoke(self, module, argv, fake):
        module.subprocess.run = fake
        original = sys.argv
        sys.argv = ['reset_cloudflare_stage.py', *argv]
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = module.main()
        finally:
            sys.argv = original
        return code, out.getvalue(), err.getvalue()

    def base_argv(self):
        return [
            '--brand', 'eddy', '--stage', 'beta',
            '--worker', 'eddy-cf-auth-beta', '--worker', 'eddy-web-beta',
            '--d1', 'app=42bbfb33', '--d1', 'auth=56f2ed9e',
            '--wrangler', __file__,
        ]

    def test_plan_mode_touches_nothing(self):
        module = load_module()
        fake = FakeWrangler({})
        code, out, _ = self.invoke(module, self.base_argv(), fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.commands, [])
        self.assertIn('delete 2 Worker(s)', out)
        self.assertIn('empty 2 D1 authorit(ies)', out)
        self.assertIn('Plan only', out)

    def test_apply_requires_the_exact_confirmation_token(self):
        module = load_module()
        fake = FakeWrangler({})
        code, _, err = self.invoke(module, [*self.base_argv(), '--apply', '--confirm', 'reset:eddy:production'], fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.commands, [])
        self.assertIn("--confirm must be exactly 'reset:eddy:beta'", err)

    def test_apply_empties_the_stage_and_verifies_it(self):
        module = load_module()
        fake = FakeWrangler({
            '42bbfb33': [{'type': 'table', 'name': 'd1_migrations'}],
            '56f2ed9e': [{'type': 'table', 'name': 'users'}],
        })
        code, out, err = self.invoke(
            module, [*self.base_argv(), '--apply', '--confirm', 'reset:eddy:beta'], fake
        )
        self.assertEqual(code, 0, err)
        deletes = [command for command in fake.commands if command[1] == 'delete']
        self.assertEqual(
            [command[2] for command in deletes], ['eddy-cf-auth-beta', 'eddy-web-beta']
        )
        self.assertIn('remaining 0', out)
        self.assertIn('Reset complete', out)
        self.assertEqual(fake.catalogues, {'42bbfb33': [], '56f2ed9e': []})

    def test_a_failed_deletion_is_reported_and_exits_nonzero(self):
        module = load_module()

        class Denied(FakeWrangler):
            def __call__(self, command, **kwargs):
                args = [str(part) for part in command[1:]]
                if args[:1] == ['delete']:
                    self.commands.append([str(part) for part in command])
                    return Result(returncode=1, stderr='Authentication error [code: 10000]')
                return super().__call__(command, **kwargs)

        fake = Denied({'42bbfb33': [], '56f2ed9e': []})
        code, _, err = self.invoke(
            module, [*self.base_argv(), '--apply', '--confirm', 'reset:eddy:beta'], fake
        )
        self.assertEqual(code, 1)
        self.assertIn('RESET INCOMPLETE', err)


if __name__ == '__main__':
    unittest.main(verbosity=2)
