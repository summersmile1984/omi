#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Create/restore deterministic volume archives and verify backup manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk():
            raise RuntimeError(f'unsafe archive member: {member.name}')
    return members


def backup(source: Path, archive_path: Path) -> None:
    if not source.is_dir():
        raise RuntimeError(f'volume source is not a directory: {source}')
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, 'w:gz') as archive:
        for child in sorted(source.iterdir(), key=lambda path: path.name):
            archive.add(child, arcname=child.name, recursive=True)
    os.chmod(archive_path, 0o600)


def restore(source: Path, archive_path: Path) -> None:
    if source.resolve() == Path('/'):
        raise RuntimeError('refusing to restore over filesystem root')
    source.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = _safe_members(archive)
        for child in source.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        archive.extractall(source, members=members)


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise RuntimeError(f'{label} must be a 64-character lowercase SHA-256 fingerprint')
    return value


def _require_private_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise RuntimeError(f'{label} is missing: {path.name}')
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError(f'{label} must be mode 0600: {path.name}')


def _safe_artifact_name(name: object) -> str:
    if not isinstance(name, str) or Path(name).name != name:
        raise RuntimeError(f'unsafe manifest artifact name: {name}')
    return name


def write_manifest(
    directory: Path,
    git_sha: str,
    files: list[str],
    runtime_fingerprint: str,
    config_fingerprint: str,
    migration_fingerprint: str,
) -> None:
    fingerprints = {
        'runtime_fingerprint': _require_sha256(runtime_fingerprint, 'runtime fingerprint'),
        'config_fingerprint': _require_sha256(config_fingerprint, 'config fingerprint'),
        'migration_fingerprint': _require_sha256(migration_fingerprint, 'migration fingerprint'),
    }
    entries = {}
    for name in files:
        _safe_artifact_name(name)
        path = directory / name
        if not path.is_file():
            raise RuntimeError(f'backup artifact missing: {name}')
        os.chmod(path, 0o600)
        entries[name] = {'sha256': _sha256(path), 'bytes': path.stat().st_size}
    payload = {
        'schema_version': 2,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'git_sha': git_sha,
        **fingerprints,
        'artifacts': entries,
    }
    manifest_path = directory / 'manifest.json'
    descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    os.chmod(manifest_path, 0o600)


def verify_manifest(
    directory: Path,
    expected_files: list[str] | None = None,
    expected_fingerprints: dict[str, str] | None = None,
) -> None:
    manifest_path = directory / 'manifest.json'
    _require_private_file(manifest_path, 'backup manifest')
    try:
        payload = json.loads(manifest_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise RuntimeError('invalid backup manifest') from error
    if payload.get('schema_version') != 2 or not isinstance(payload.get('artifacts'), dict):
        raise RuntimeError('unsupported backup manifest')
    for key in ('runtime_fingerprint', 'config_fingerprint', 'migration_fingerprint'):
        _require_sha256(payload.get(key), key)
    if expected_fingerprints is not None:
        if set(expected_fingerprints) != {'runtime_fingerprint', 'config_fingerprint', 'migration_fingerprint'}:
            raise RuntimeError('expected backup fingerprints are incomplete')
        for key, expected in expected_fingerprints.items():
            if _require_sha256(expected, f'expected {key}') != payload[key]:
                raise RuntimeError(f'backup {key} does not match the current deployment')
    if expected_files is not None:
        expected = {_safe_artifact_name(name) for name in expected_files}
        if set(payload['artifacts']) != expected:
            raise RuntimeError('backup manifest artifact set does not match the required backup artifacts')
    for name, metadata in payload['artifacts'].items():
        _safe_artifact_name(name)
        if not isinstance(metadata, dict):
            raise RuntimeError(f'invalid backup manifest metadata: {name}')
        path = directory / name
        _require_private_file(path, 'backup artifact')
        if (
            _require_sha256(metadata.get('sha256'), f'backup artifact checksum for {name}') != _sha256(path)
            or not isinstance(metadata.get('bytes'), int)
            or isinstance(metadata.get('bytes'), bool)
            or metadata['bytes'] != path.stat().st_size
        ):
            raise RuntimeError(f'backup checksum mismatch: {name}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    for command in ('backup', 'restore'):
        child = subparsers.add_parser(command)
        child.add_argument('source', type=Path)
        child.add_argument('archive', type=Path)
    manifest = subparsers.add_parser('manifest')
    manifest.add_argument('directory', type=Path)
    manifest.add_argument('--git-sha', required=True)
    manifest.add_argument('--runtime-fingerprint', required=True)
    manifest.add_argument('--config-fingerprint', required=True)
    manifest.add_argument('--migration-fingerprint', required=True)
    manifest.add_argument('files', nargs='+')
    verify = subparsers.add_parser('verify')
    verify.add_argument('directory', type=Path)
    verify.add_argument('--expected-files', nargs='+')
    verify.add_argument('--expected-runtime-fingerprint', required=True)
    verify.add_argument('--expected-config-fingerprint', required=True)
    verify.add_argument('--expected-migration-fingerprint', required=True)
    args = parser.parse_args()
    if args.command == 'backup':
        backup(args.source, args.archive)
    elif args.command == 'restore':
        restore(args.source, args.archive)
    elif args.command == 'manifest':
        write_manifest(
            args.directory,
            args.git_sha,
            args.files,
            args.runtime_fingerprint,
            args.config_fingerprint,
            args.migration_fingerprint,
        )
    else:
        verify_manifest(
            args.directory,
            args.expected_files,
            {
                'runtime_fingerprint': args.expected_runtime_fingerprint,
                'config_fingerprint': args.expected_config_fingerprint,
                'migration_fingerprint': args.expected_migration_fingerprint,
            },
        )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
