#!/usr/bin/env python3
"""Provision the selected public model bundle; runtime itself never downloads."""

import argparse
import hashlib
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'scripts/profiles'))
from fork.model_contract import validate_speech
from fork.speech_assets import manifest_bytes, verify
from render import resolve


def extract(archive, output):
    with tarfile.open(archive, 'r:bz2') as bundle:
        total = 0
        members = bundle.getmembers()
        for member in members:
            if not (member.isfile() or member.isdir()) or not (output / member.name).resolve().is_relative_to(output.resolve()):
                raise ValueError('model archive contains unsafe paths or non-file entries')
            total += member.size
            if total > 1_500_000_000:
                raise ValueError('model archive exceeds unpacked size bound')
        bundle.extractall(output, members=members, filter='data')


def provision(output: Path, contract):
    if output.exists():
        verify(output, contract)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.speech-', dir=output.parent) as temporary:
        staging = Path(temporary)
        for kind, model, expected in (
            ('asr', contract.stt_model, contract.stt_archive_digest),
            ('tts', contract.tts_model, contract.tts_archive_digest),
        ):
            archive = staging / (kind + '.tar.bz2')
            url = f'https://github.com/k2-fsa/sherpa-onnx/releases/download/{kind}-models/{model}.tar.bz2'
            digest, size = hashlib.sha256(), 0
            with urllib.request.urlopen(url, timeout=60) as response, archive.open('wb') as stream:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 512_000_000:
                        raise ValueError('model archive exceeds download size bound')
                    digest.update(chunk)
                    stream.write(chunk)
            if 'sha256:' + digest.hexdigest() != expected:
                raise ValueError('downloaded model archive digest does not match the profile')
            extract(archive, staging)
            archive.unlink()
        (staging / 'manifest.json').write_bytes(manifest_bytes(staging, contract))
        verify(staging, contract)
        # Never overwrite/rebind an existing model directory.
        os.rename(staging, output)
    print('Verified speech bundle ready for a read-only runtime mount.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    rows = resolve('self_hosted', stage='local')['profiles']
    contract = validate_speech(rows['self_hosted.local'].get('speech'))
    if contract is None:
        parser.error('the selected target explicitly disables speech')
    provision(args.output.resolve(), contract)


if __name__ == '__main__':
    main()
