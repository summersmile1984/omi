"""Model publication and image runtime behavior; no live model in CI."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'deploy/self-host' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value):
    return 'sha256:' + hashlib.sha256(value).hexdigest()


@pytest.mark.parametrize('corrupt', [False, True])
def test_model_store_publishes_only_fully_verified_blobs(tmp_path, monkeypatch, corrupt):
    module = script('prepare-model')
    config, model = b'{"format":"gguf"}', b'synthetic model bytes'
    manifest = json.dumps(
        {
            'config': {'digest': digest(config), 'size': len(config)},
            'layers': [
                {'digest': digest(model), 'size': len(model), 'mediaType': 'application/vnd.ollama.image.model'}
            ],
        }
    ).encode()
    contract = SimpleNamespace(model='test:fixed', manifest_digest=digest(manifest), artifact_digest=digest(model))
    served = {'fixed': manifest, digest(config): config, digest(model): b'wrong' if corrupt else model}
    requests = []

    def fetch(url, timeout):
        requests.append(url)
        return io.BytesIO(served[url.rsplit('/', 1)[-1]])

    monkeypatch.setattr(module.urllib.request, 'urlopen', fetch)
    target = tmp_path / 'store'
    if corrupt:
        with pytest.raises(ValueError, match='size or digest'):
            module.provision(target, contract)
        assert not target.exists() and list(tmp_path.iterdir()) == []
    else:
        proof = module.provision(target, contract)
        assert proof['verified_blobs'] == 2
        assert target.stat().st_mode & 0o777 == 0o755
        assert all(path.stat().st_mode & 0o444 == 0o444 for path in target.rglob('*') if path.is_file())
        before = list(requests)
        assert module.provision(target, contract) == proof
        assert requests == before  # Existing stores are rehashed, never downloaded over.
        next(target.glob('blobs/sha256-*')).write_bytes(b'changed')
        with pytest.raises(ValueError, match='size or checksum'):
            module.provision(target, contract)
