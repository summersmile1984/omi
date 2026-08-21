#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Create, authenticate, and restore self-host backup envelopes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_SCHEMA_VERSION = 3
MANIFEST_FINGERPRINT_KEYS = (
    'runtime_fingerprint',
    'config_fingerprint',
    'migration_fingerprint',
)
ENVELOPE_FORMAT = 'omi-backup-aead-v1'
ENVELOPE_MAGIC = b'OMI-BACKUP-AEAD-V1\n'
ENVELOPE_CHUNK_BYTES = 4 * 1024 * 1024
ENVELOPE_KEY_BYTES = 32
ENVELOPE_SALT_BYTES = 16
ENVELOPE_NONCE_BYTES = 12
ENVELOPE_TAG_BYTES = 16
_HEADER = struct.Struct('>I16s')
_LENGTH = struct.Struct('>I')
_HKDF_INFO = b'omi-self-host-backup-aead-v1'


def _protect_and_hash_file(path: Path, label: str) -> tuple[str, int]:
    """Hash one regular artifact while changing its mode through the open fd."""

    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RuntimeError(f'{label} is missing: {path.name}') from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f'{label} must be a regular file: {path.name}')
        os.fchmod(descriptor, 0o600)
        digest = hashlib.sha256()
        with os.fdopen(descriptor, 'rb') as handle:
            descriptor = -1
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest(), metadata.st_size
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_exact(handle, size: int, label: str) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = handle.read(remaining)
        if not chunk:
            raise RuntimeError(f'truncated backup envelope {label}')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = Path(member.name)
        if (
            path.is_absolute()
            or '..' in path.parts
            or member.issym()
            or member.islnk()
            or member.ischr()
            or member.isblk()
            or member.isfifo()
            or not (member.isdir() or member.isreg())
        ):
            raise RuntimeError(f'unsafe archive member: {member.name}')
    return members


def _require_private_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f'{label} is missing: {path.name}')
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError(f'{label} must be mode 0600: {path.name}')


def _load_key(path: Path) -> bytes:
    _require_private_file(path, 'backup key')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RuntimeError(f'backup key is missing: {path.name}') from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f'backup key is missing: {path.name}')
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError(f'backup key must be mode 0600: {path.name}')
        with os.fdopen(descriptor, 'rb') as handle:
            descriptor = -1
            key = handle.read(ENVELOPE_KEY_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(key) != ENVELOPE_KEY_BYTES:
        raise RuntimeError('backup key must contain exactly 32 bytes')
    return key


def _derive_key(key: bytes, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=SHA256(), length=ENVELOPE_KEY_BYTES, salt=salt, info=_HKDF_INFO).derive(key)


def _associated_data(header: bytes, index: int, length: int) -> bytes:
    return header + struct.pack('>QI', index, length)


class _EnvelopeWriter:
    """A file-like sink that encrypts bounded chunks as callers write bytes."""

    def __init__(self, path: Path, key: bytes) -> None:
        if not path.name.endswith('.enc'):
            raise RuntimeError('encrypted backup artifacts must use the .enc suffix')
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(descriptor, 0o600)
        self._handle = os.fdopen(descriptor, 'wb')
        salt = os.urandom(ENVELOPE_SALT_BYTES)
        self._header = ENVELOPE_MAGIC + _HEADER.pack(ENVELOPE_CHUNK_BYTES, salt)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._aes = AESGCM(_derive_key(key, salt))
        self._buffer = bytearray()
        self._index = 0
        self._closed = False
        self._handle.write(self._header)

    def write(self, data: bytes) -> int:
        if self._closed:
            raise RuntimeError('backup envelope writer is closed')
        self._buffer.extend(data)
        while len(self._buffer) >= ENVELOPE_CHUNK_BYTES:
            self._emit(bytes(self._buffer[:ENVELOPE_CHUNK_BYTES]))
            del self._buffer[:ENVELOPE_CHUNK_BYTES]
        return len(data)

    def flush(self) -> None:
        self._handle.flush()

    def _emit(self, plaintext: bytes) -> None:
        nonce = os.urandom(ENVELOPE_NONCE_BYTES)
        ciphertext = self._aes.encrypt(nonce, plaintext, _associated_data(self._header, self._index, len(plaintext)))
        self._handle.write(_LENGTH.pack(len(plaintext)))
        self._handle.write(nonce)
        self._handle.write(ciphertext)
        self._index += 1

    def close(self) -> None:
        if self._closed:
            return
        if self._buffer:
            self._emit(bytes(self._buffer))
            self._buffer.clear()
        self._handle.write(_LENGTH.pack(0))
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        self._closed = True

    def abort(self) -> None:
        if not self._closed:
            self._handle.close()
            self._closed = True
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> '_EnvelopeWriter':
        return self

    def __exit__(self, exc_type, _exc_value, _traceback) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()


def _iter_decrypted(path: Path, key: bytes):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _require_private_file(path, 'backup artifact')
    with path.open('rb') as handle:
        magic = _read_exact(handle, len(ENVELOPE_MAGIC), 'header')
        if magic != ENVELOPE_MAGIC:
            raise RuntimeError('unsupported backup envelope format')
        header_fields = _read_exact(handle, _HEADER.size, 'header')
        chunk_bytes, salt = _HEADER.unpack(header_fields)
        if chunk_bytes != ENVELOPE_CHUNK_BYTES:
            raise RuntimeError('unsupported backup envelope chunk size')
        header = magic + header_fields
        aes = AESGCM(_derive_key(key, salt))
        index = 0
        while True:
            length = _LENGTH.unpack(_read_exact(handle, _LENGTH.size, 'record'))[0]
            if length == 0:
                if handle.read(1):
                    raise RuntimeError('backup envelope has trailing bytes')
                return
            if length > ENVELOPE_CHUNK_BYTES:
                raise RuntimeError('backup envelope record is too large')
            nonce = _read_exact(handle, ENVELOPE_NONCE_BYTES, 'record')
            ciphertext = _read_exact(handle, length + ENVELOPE_TAG_BYTES, 'record')
            try:
                plaintext = aes.decrypt(nonce, ciphertext, _associated_data(header, index, length))
            except InvalidTag as error:
                raise RuntimeError('backup authentication failed') from error
            if len(plaintext) != length:
                raise RuntimeError('backup envelope plaintext length mismatch')
            yield plaintext
            index += 1


def _verify_envelope(path: Path, key: bytes) -> None:
    for _ in _iter_decrypted(path, key):
        pass


@contextmanager
def _decrypted_temp(path: Path, key: bytes):
    descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.restore', dir=path.parent)
    temporary = Path(temporary_name)
    os.fchmod(descriptor, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            for chunk in _iter_decrypted(path, key):
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def backup(source: Path, archive_path: Path, key_file: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise RuntimeError(f'volume source is not a directory: {source}')
    for current, directories, files in os.walk(source, followlinks=False):
        for name in (*directories, *files):
            child = Path(current) / name
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise RuntimeError(f'volume source contains an unsafe member: {child.name}')
    key = _load_key(key_file)
    try:
        with _EnvelopeWriter(archive_path, key) as encrypted:
            with tarfile.open(fileobj=encrypted, mode='w|gz') as archive:
                for child in sorted(source.iterdir(), key=lambda path: path.name):
                    archive.add(child, arcname=child.name, recursive=True)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def _clear_directory(directory: Path) -> None:
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def restore(source: Path, archive_path: Path, key_file: Path) -> None:
    if source.resolve() == Path('/'):
        raise RuntimeError('refusing to restore over filesystem root')
    if source.exists() and (source.is_symlink() or not source.is_dir()):
        raise RuntimeError(f'restore target is not a directory: {source}')
    if not archive_path.name.endswith('.enc'):
        raise RuntimeError('restore requires an encrypted .enc artifact')
    key = _load_key(key_file)
    source.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f'.{source.name}.restore-', dir=source.parent))
    try:
        with _decrypted_temp(archive_path, key) as plaintext:
            with tarfile.open(plaintext, 'r:gz') as archive:
                members = _safe_members(archive)
                archive.extractall(staging, members=members)
        source.mkdir(parents=True, exist_ok=True)
        _clear_directory(source)
        for child in staging.iterdir():
            shutil.move(str(child), str(source / child.name))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def seal_file(source: Path, archive_path: Path, key_file: Path) -> None:
    key = _load_key(key_file)
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f'plaintext backup source is missing: {source.name}')
    try:
        with source.open('rb') as handle, _EnvelopeWriter(archive_path, key) as encrypted:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                encrypted.write(chunk)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def seal_stdin(archive_path: Path, key_file: Path) -> None:
    key = _load_key(key_file)
    try:
        with _EnvelopeWriter(archive_path, key) as encrypted:
            for chunk in iter(lambda: sys.stdin.buffer.read(1024 * 1024), b''):
                encrypted.write(chunk)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def open_file(source: Path, plaintext_path: Path, key_file: Path) -> None:
    if plaintext_path.exists():
        raise RuntimeError(f'plaintext restore target already exists: {plaintext_path.name}')
    key = _load_key(key_file)
    descriptor = os.open(plaintext_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            for chunk in _iter_decrypted(source, key):
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        plaintext_path.unlink(missing_ok=True)
        raise


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise RuntimeError(f'{label} must be a 64-character lowercase SHA-256 fingerprint')
    return value


def _safe_artifact_name(name: object) -> str:
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise RuntimeError(f'unsafe manifest artifact name: {name}')
    if not name.endswith('.enc'):
        raise RuntimeError(f'backup artifact must be encrypted: {name}')
    return name


def _require_git_sha(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError('git_sha must be a non-empty source revision')
    return value


def write_manifest(
    directory: Path,
    git_sha: str,
    files: list[str],
    runtime_fingerprint: str,
    config_fingerprint: str,
    migration_fingerprint: str,
) -> None:
    _require_git_sha(git_sha)
    fingerprints = {
        'runtime_fingerprint': _require_sha256(runtime_fingerprint, 'runtime fingerprint'),
        'config_fingerprint': _require_sha256(config_fingerprint, 'config fingerprint'),
        'migration_fingerprint': _require_sha256(migration_fingerprint, 'migration fingerprint'),
    }
    entries = {}
    for name in files:
        name = _safe_artifact_name(name)
        if name in entries:
            raise RuntimeError(f'duplicate backup artifact: {name}')
        path = directory / name
        if not path.is_file():
            raise RuntimeError(f'backup artifact missing: {name}')
        if path.is_symlink():
            raise RuntimeError(f'backup artifact must not be a symlink: {name}')
        sha256, bytes_count = _protect_and_hash_file(path, 'backup artifact')
        entries[name] = {'sha256': sha256, 'bytes': bytes_count}
    payload = {
        'schema_version': MANIFEST_SCHEMA_VERSION,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'git_sha': git_sha,
        **fingerprints,
        'encryption': {'format': ENVELOPE_FORMAT, 'chunk_bytes': ENVELOPE_CHUNK_BYTES},
        'artifacts': entries,
    }
    manifest_path = directory / 'manifest.json'
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(manifest_path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + '\n')


def verify_manifest(
    directory: Path,
    expected_files: list[str] | None = None,
    expected_fingerprints: dict[str, str] | None = None,
    key_file: Path | None = None,
) -> None:
    if key_file is None:
        raise RuntimeError('backup verification requires an explicit key file')
    manifest_path = directory / 'manifest.json'
    _require_private_file(manifest_path, 'backup manifest')
    try:
        payload = json.loads(manifest_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise RuntimeError('invalid backup manifest') from error
    if (
        not isinstance(payload, dict)
        or payload.get('schema_version') != MANIFEST_SCHEMA_VERSION
        or not isinstance(payload.get('artifacts'), dict)
        or payload.get('encryption') != {'format': ENVELOPE_FORMAT, 'chunk_bytes': ENVELOPE_CHUNK_BYTES}
    ):
        raise RuntimeError('unsupported backup manifest')
    if not isinstance(payload.get('created_at'), str) or not payload['created_at']:
        raise RuntimeError('backup manifest created_at is missing')
    _require_git_sha(payload.get('git_sha'))
    for key in MANIFEST_FINGERPRINT_KEYS:
        _require_sha256(payload.get(key), key)
    if expected_fingerprints is not None:
        if set(expected_fingerprints) != set(MANIFEST_FINGERPRINT_KEYS):
            raise RuntimeError('expected backup fingerprints are incomplete')
        for key, expected in expected_fingerprints.items():
            if _require_sha256(expected, f'expected {key}') != payload[key]:
                raise RuntimeError(f'backup {key} does not match the current deployment')
    if expected_files is not None:
        expected = {_safe_artifact_name(name) for name in expected_files}
        if set(payload['artifacts']) != expected:
            raise RuntimeError('backup manifest artifact set does not match the required backup artifacts')
    key = _load_key(key_file)
    for name, metadata in payload['artifacts'].items():
        _safe_artifact_name(name)
        if not isinstance(metadata, dict):
            raise RuntimeError(f'invalid backup manifest metadata: {name}')
        path = directory / name
        _require_private_file(path, 'backup artifact')
        actual_sha256, actual_bytes = _protect_and_hash_file(path, 'backup artifact')
        if (
            _require_sha256(metadata.get('sha256'), f'backup artifact checksum for {name}') != actual_sha256
            or not isinstance(metadata.get('bytes'), int)
            or isinstance(metadata.get('bytes'), bool)
            or metadata['bytes'] != actual_bytes
        ):
            raise RuntimeError(f'backup checksum mismatch: {name}')
        _verify_envelope(path, key)


def _key_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--key-file', type=Path, required=True, help='mode-0600 file containing exactly 32 random bytes'
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    for command in ('backup', 'restore'):
        child = subparsers.add_parser(command)
        child.add_argument('source', type=Path)
        child.add_argument('archive', type=Path)
        _key_argument(child)
    seal = subparsers.add_parser('seal')
    seal.add_argument('source', type=Path)
    seal.add_argument('archive', type=Path)
    _key_argument(seal)
    seal_stdin_parser = subparsers.add_parser('seal-stdin')
    seal_stdin_parser.add_argument('archive', type=Path)
    _key_argument(seal_stdin_parser)
    open_parser = subparsers.add_parser('open')
    open_parser.add_argument('archive', type=Path)
    open_parser.add_argument('plaintext', type=Path)
    _key_argument(open_parser)
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
    _key_argument(verify)
    args = parser.parse_args()
    try:
        if args.command == 'backup':
            backup(args.source, args.archive, args.key_file)
        elif args.command == 'restore':
            restore(args.source, args.archive, args.key_file)
        elif args.command == 'seal':
            seal_file(args.source, args.archive, args.key_file)
        elif args.command == 'seal-stdin':
            seal_stdin(args.archive, args.key_file)
        elif args.command == 'open':
            open_file(args.archive, args.plaintext, args.key_file)
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
                args.key_file,
            )
    except (OSError, RuntimeError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
