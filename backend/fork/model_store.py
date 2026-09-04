"""Validate the selected, read-only Ollama model store before starting its service."""

import argparse
import hashlib
import json
from pathlib import Path

from .model_contract import validate
from .profile import current


def _digest(path):
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256')
    return 'sha256:' + digest.hexdigest()


def check(root, contract):
    root = Path(root).resolve(strict=True)
    # Ollama's registry layout; explicit tags are mandatory, no implicit latest.
    name, tag = contract.model.rsplit(':', 1)
    parts = name.split('/')
    if len(parts) == 1:
        parts.insert(0, 'library')
    if len(parts) == 2:
        parts.insert(0, 'registry.ollama.ai')
    manifest = root.joinpath('manifests', *parts, tag)
    if _digest(manifest) != contract.manifest_digest:
        raise ValueError('embedding manifest checksum differs')
    document = json.loads(manifest.read_text())
    layers = [document['config'], *document['layers']]
    models = [
        layer['digest'] for layer in document['layers'] if layer['mediaType'] == 'application/vnd.ollama.image.model'
    ]
    if models != [contract.artifact_digest]:
        raise ValueError('embedding model artifact differs from manifest')
    for layer in layers:
        digest = layer['digest']
        # Reuse the digest validator, including path traversal rejection.
        validate({**contract.as_dict(), 'artifact_digest': digest})
        blob = root / 'blobs' / digest.replace(':', '-')
        if blob.stat().st_size != layer['size'] or _digest(blob) != digest:
            raise ValueError('embedding blob size or checksum differs')
    return {
        'model': contract.model,
        'manifest_digest': contract.manifest_digest,
        'artifact_digest': contract.artifact_digest,
        'verified_blobs': len(layers),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.root, validate(current().get('embedding')))))


if __name__ == '__main__':
    main()
