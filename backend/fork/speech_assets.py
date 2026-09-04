"""Content-addressed speech bundle admission shared by provisioning and serving."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .model_contract import SpeechContract


def manifest_bytes(root: Path, contract: SpeechContract) -> bytes:
    files = []
    for model in (contract.stt_model, contract.tts_model):
        directory = root / model
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError('speech model directory is missing or not a real directory')
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():
                raise ValueError('speech artifacts cannot contain symbolic links')
            if path.is_file():
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                files.append({'path': path.relative_to(root).as_posix(), 'size': path.stat().st_size, 'sha256': digest})
    return (json.dumps({'files': files}, sort_keys=True, separators=(',', ':')) + '\n').encode()


def verify(root: Path, contract: SpeechContract) -> None:
    # Recompute the complete immutable model tree, including lexicons, voices,
    # licenses and espeak data. A trusted manifest must never bless extra files.
    encoded = manifest_bytes(root, contract)
    if 'sha256:' + hashlib.sha256(encoded).hexdigest() != contract.bundle_digest:
        raise ValueError('speech model bundle content does not match the selected profile')
    marker = root / 'manifest.json'
    if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != encoded:
        raise ValueError('speech model manifest is missing or differs from its content-addressed tree')
