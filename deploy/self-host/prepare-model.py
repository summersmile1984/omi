#!/usr/bin/env python3
"""Provision one profile-selected Ollama store; serving never downloads models."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'scripts/profiles'))
from fork.model_contract import validate, validate_digest, validate_llm
from fork.model_store import check
from render import resolve


def download(url, output, digest, size):
    validate_digest(digest)
    if type(size) is not int or not 0 < size <= 4_000_000_000:
        raise ValueError('model download size exceeds the admitted bound')
    actual, count = hashlib.sha256(), 0
    with urllib.request.urlopen(url, timeout=60) as response, output.open('xb') as stream:
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > size:
                raise ValueError('model download exceeds its manifest size')
            actual.update(chunk)
            stream.write(chunk)
    if count != size or 'sha256:' + actual.hexdigest() != digest:
        raise ValueError('downloaded model size or digest differs from the profile')


def provision(output, contract):
    if output.exists():
        return check(output, contract)
    name, tag = contract.model.rsplit(':', 1)
    parts = name.split('/')
    if len(parts) == 1:
        parts.insert(0, 'library')
    if len(parts) != 2:
        raise ValueError('provisioning only admits the public registry.ollama.ai model authority')
    api = 'https://registry.ollama.ai/v2/' + '/'.join(parts)
    with urllib.request.urlopen(api + '/manifests/' + tag, timeout=60) as response:
        content = response.read(65537)
    if len(content) > 65536 or 'sha256:' + hashlib.sha256(content).hexdigest() != contract.manifest_digest:
        raise ValueError('model manifest differs from the profile')
    manifest = json.loads(content)
    layers = [manifest['config'], *manifest['layers']]
    model_layers = [
        layer['digest'] for layer in manifest['layers'] if layer['mediaType'] == 'application/vnd.ollama.image.model'
    ]
    if model_layers != [contract.artifact_digest] or len(layers) > 32:
        raise ValueError('manifest model identity or layer count differs')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.model-', dir=output.parent) as temporary:
        stage = Path(temporary)
        target = stage.joinpath('manifests', 'registry.ollama.ai', *parts, tag)
        target.parent.mkdir(parents=True)
        target.write_bytes(content)
        (stage / 'blobs').mkdir()
        for layer in layers:
            digest = validate_digest(layer['digest'])
            download(api + '/blobs/' + digest, stage / 'blobs' / digest.replace(':', '-'), digest, layer['size'])
        proof = check(stage, contract)
        # The runtime admission process is non-root; these public model files
        # must stay readable across the host/container UID boundary.
        for entry in [stage, *stage.rglob('*')]:
            entry.chmod(0o755 if entry.is_dir() else 0o644)
        os.rename(stage, output)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('embedding', 'llm'), required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    row = resolve('self_hosted', stage='local')['profiles']['self_hosted.local']
    contract = (validate if args.kind == 'embedding' else validate_llm)(row.get(args.kind))
    if contract is None:
        parser.error('the selected model capability is disabled')
    print(json.dumps(provision(args.output.resolve(), contract)))


if __name__ == '__main__':
    main()
