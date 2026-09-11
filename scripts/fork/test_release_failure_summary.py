#!/usr/bin/env python3
"""Behavioral tests for the release failure evidence step.

The command runs on an already-failing job, so the properties that matter are:
it finds exactly the journals belonging to the admitted source, it reports a
missing or malformed journal instead of raising, and it exits 0 in every case.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'release_failure_summary.py'
SHA = 'f843a224981e06c323c8392c80c2c38951513f76'
OTHER = 'acf2b8406641db9c12f46836d118c462239dcc39'


def write_journal(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding='utf-8')


class ReleaseFailureSummaryTests(unittest.TestCase):
    def run_script(
        self,
        root: Path,
        *,
        sha: str = SHA,
        target: str = 'cloudflare',
        summary: Path | None = None,
    ) -> subprocess.CompletedProcess:
        evidence = root.parent / f'evidence-{target}'
        command = [
            sys.executable,
            str(SCRIPT),
            '--target',
            target,
            '--stage',
            'beta',
            '--sha',
            sha,
            '--journal-root',
            str(root),
            '--evidence-dir',
            str(evidence),
        ]
        if summary is not None:
            command += ['--summary', str(summary)]
        return subprocess.run(command, capture_output=True, text=True)

    def test_reports_and_copies_only_the_admitted_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'journals'
            write_journal(
                root / f'beta-{SHA}-11111111-1111-1111-1111-111111111111' / 'journal.json',
                {
                    'state': 'recovery_required',
                    'release_ready': False,
                    'failure': {
                        'target': 'cloudflare',
                        'stage': 'beta',
                        'error_name': 'Error',
                        'reason': 'release readiness did not report ready JSON: auth',
                    },
                },
            )
            write_journal(
                root / f'beta-{OTHER}-22222222-2222-2222-2222-222222222222' / 'journal.json',
                {'state': 'completed', 'release_ready': True},
            )
            summary = Path(directory) / 'summary.md'
            summary.write_text('', encoding='utf-8')

            proc = self.run_script(root, summary=summary)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = summary.read_text(encoding='utf-8')
            self.assertIn('### cloudflare release failure evidence', report)
            self.assertIn('release readiness did not report ready JSON: auth', report)
            self.assertIn('state=`recovery_required`', report)
            # The other source's completed journal is neither reported nor copied.
            self.assertNotIn(OTHER, report)
            evidence = sorted(path.name for path in (Path(directory) / 'evidence-cloudflare').iterdir())
            self.assertEqual(evidence, [f'beta-{SHA}-11111111-1111-1111-1111-111111111111.json'])

    def test_server_layouts_are_both_searched(self):
        for layout in ('releases', 'qualifications'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'server'
                write_journal(
                    root / layout / SHA / 'journal.json',
                    {
                        'state': 'failed-reconciliation-required',
                        'release_ready': False,
                        'failure': {
                            'target': 'self_hosted',
                            'stage': 'beta',
                            'error_name': 'RuntimeError',
                            'reason': 'backend readiness rejected Bearer ***',
                        },
                    },
                )
                proc = self.run_script(root, target='self_hosted')
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn('### self_hosted release failure evidence', proc.stdout)
                self.assertIn('backend readiness rejected Bearer ***', proc.stdout)

    def test_missing_root_is_reported_without_failing_the_step(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = self.run_script(Path(directory) / 'does-not-exist')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('No retained journal matched this source', proc.stdout)
            self.assertFalse((Path(directory) / 'evidence-cloudflare').exists())

    def test_malformed_journal_is_reported_without_failing_the_step(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'journals'
            write_journal(root / f'beta-{SHA}-33333333-3333-3333-3333-333333333333' / 'journal.json', '{not json')
            proc = self.run_script(root)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('could not be read', proc.stdout)

    def test_journal_without_a_failure_block_says_so(self):
        # A run that failed before the transaction started cannot have a failure
        # record; the report must say that rather than implying the journal is
        # complete.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'journals'
            write_journal(
                root / f'beta-{SHA}-44444444-4444-4444-4444-444444444444' / 'journal.json',
                {'state': 'admitted', 'release_ready': False},
            )
            proc = self.run_script(root)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('no `failure` block', proc.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
