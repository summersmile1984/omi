#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'self-host' / 'volume-snapshot.py'
OPERATIONS = SCRIPT.with_name('operations.sh')
SPEC = importlib.util.spec_from_file_location('self_host_volume_snapshot', SCRIPT)
assert SPEC and SPEC.loader
SNAPSHOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SNAPSHOT)


class SelfHostOperationsTest(unittest.TestCase):
    def test_operations_entrypoint_self_check(self) -> None:
        result = subprocess.run(
            ['bash', str(OPERATIONS), 'self-check'],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('self-host operations self-check OK', result.stdout)

    def test_volume_backup_restore_and_manifest_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'state'
            source.mkdir()
            (source / 'nested').mkdir()
            (source / 'nested' / 'record.json').write_text('{"version":1}\n', encoding='utf-8')
            archive = root / 'state.tar.gz'

            SNAPSHOT.backup(source, archive)
            SNAPSHOT.write_manifest(root, 'deadbeef', [archive.name])
            SNAPSHOT.verify_manifest(root)

            (source / 'nested' / 'record.json').write_text('corrupt', encoding='utf-8')
            (source / 'stale').write_text('must disappear', encoding='utf-8')
            SNAPSHOT.restore(source, archive)
            self.assertEqual((source / 'nested' / 'record.json').read_text(encoding='utf-8'), '{"version":1}\n')
            self.assertFalse((source / 'stale').exists())

            archive.write_bytes(archive.read_bytes() + b'tampered')
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                SNAPSHOT.verify_manifest(root)

    def test_restore_rejects_archive_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'state'
            source.mkdir()
            archive_path = root / 'unsafe.tar.gz'
            with tarfile.open(archive_path, 'w:gz') as archive:
                member = tarfile.TarInfo('../outside')
                member.size = 1
                archive.addfile(member, io.BytesIO(b'x'))

            with self.assertRaisesRegex(RuntimeError, 'unsafe archive member'):
                SNAPSHOT.restore(source, archive_path)

    def test_manifest_records_source_revision_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / 'postgres.dump'
            artifact.write_bytes(b'dump')
            SNAPSHOT.write_manifest(root, 'cafebabe', [artifact.name])

            payload = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(payload['git_sha'], 'cafebabe')
            self.assertEqual(set(payload['artifacts']), {'postgres.dump'})
            self.assertNotIn('secret', json.dumps(payload).lower())

    def test_restore_and_start_static_contract_is_fail_closed(self) -> None:
        """Tripwire for the destructive ordering; live Compose proves behavior."""

        script = OPERATIONS.read_text(encoding='utf-8')
        recreate = script.index('dropdb -U "$POSTGRES_USER" --force --if-exists')
        restore = script.index("pg_restore -U \"$POSTGRES_USER\"")
        migration = script.index('compose run --rm --no-deps -T auth-migrate')
        application_start = script.index('compose up --detach --wait --no-deps "${APPLICATION_SERVICES[@]}"')
        self.assertLess(recreate, restore)
        self.assertLess(migration, application_start)


if __name__ == '__main__':
    unittest.main()
