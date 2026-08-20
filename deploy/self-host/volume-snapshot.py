#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Create/restore deterministic volume archives and verify backup manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
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


def write_manifest(directory: Path, git_sha: str, files: list[str]) -> None:
    entries = {}
    for name in files:
        path = directory / name
        if not path.is_file():
            raise RuntimeError(f'backup artifact missing: {name}')
        entries[name] = {'sha256': _sha256(path), 'bytes': path.stat().st_size}
    payload = {
        'schema_version': 1,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'git_sha': git_sha,
        'artifacts': entries,
    }
    (directory / 'manifest.json').write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def verify_manifest(directory: Path) -> None:
    manifest_path = directory / 'manifest.json'
    payload = json.loads(manifest_path.read_text(encoding='utf-8'))
    if payload.get('schema_version') != 1 or not isinstance(payload.get('artifacts'), dict):
        raise RuntimeError('unsupported backup manifest')
    for name, metadata in payload['artifacts'].items():
        if Path(name).name != name:
            raise RuntimeError(f'unsafe manifest artifact name: {name}')
        path = directory / name
        if not path.is_file() or _sha256(path) != metadata.get('sha256'):
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
    manifest.add_argument('files', nargs='+')
    verify = subparsers.add_parser('verify')
    verify.add_argument('directory', type=Path)
    args = parser.parse_args()
    if args.command == 'backup':
        backup(args.source, args.archive)
    elif args.command == 'restore':
        restore(args.source, args.archive)
    elif args.command == 'manifest':
        write_manifest(args.directory, args.git_sha, args.files)
    else:
        verify_manifest(args.directory)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
