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


def test_image_environment_overrides_ambient_precision_from_one_generated_profile(tmp_path):
    import os
    import subprocess

    from fork.llm_runtime import compile_environment, process_environment
    from fork.model_contract import validate_llm
    from fork.queue_config import QUEUES
    from fork.tests.test_local_llm import CONTRACT

    table = {
        'target': 'self_hosted',
        'profiles': {'self_hosted.local': {'llm': CONTRACT, 'capabilities': {'llm_provider': 'ollama'}}},
    }
    script_text = compile_environment(table)
    env = {**os.environ, 'OLLAMA_KV_CACHE_TYPE': 'f16', 'OLLAMA_FLASH_ATTENTION': '0', 'OLLAMA_NO_CLOUD': '0'}
    result = subprocess.run(['/bin/sh', '-c', script_text + 'env'], env=env, capture_output=True, text=True, check=True)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    assert values['OLLAMA_KV_CACHE_TYPE'] == CONTRACT['kv_cache_type']
    assert values['OLLAMA_FLASH_ATTENTION'] == '1' and values['OLLAMA_NO_CLOUD'] == '1'
    assert values['OLLAMA_CONTEXT_LENGTH'] == str(CONTRACT['context_window'])
    assert values['OLLAMA_KEEP_ALIVE'] == '0'
    runtime = process_environment(validate_llm(CONTRACT))
    assert runtime == {
        'AGENT_STREAM_FIRST_EVENT_TIMEOUT_SECONDS': '300',
        'AGENT_STREAM_MAX_DURATION_SECONDS': '600',
        'QUEUE_REDIS_FINALIZATION_REQUEST_TIMEOUT_SECONDS': '1200',
    }
    from unittest.mock import patch

    with patch.dict(os.environ, runtime):
        assert QUEUES[-1].request_timeout() == 1200
    table['profiles']['self_hosted.local']['llm'] = {**CONTRACT, 'kv_cache_type': 'unknown'}
    with pytest.raises(ValueError, match='precision'):
        compile_environment(table)
    table['profiles']['self_hosted.local'].pop('llm')
    with pytest.raises(ValueError, match='enabled'):
        compile_environment(table)
