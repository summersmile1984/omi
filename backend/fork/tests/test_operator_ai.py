"""Hermetic operator-AI contracts: frozen vendors, credentials and endpoint grants.

Real hosted-vendor inference is a separate live probe; every request here runs
through a controlled transport or fails closed before a socket is opened.
"""

from dataclasses import asdict
import json
from pathlib import Path
from unittest import mock

import httpx
import pytest

from fork import capabilities, embedding, local_llm, operator_ai, profile, speech


def selected():
    # Load the production profile renderer, not a second model identity fixture.
    import sys
    from importlib.util import spec_from_file_location, module_from_spec

    root = Path(__file__).resolve().parents[3]
    with mock.patch.object(sys, 'path', [str(root / 'scripts/profiles'), *sys.path]):
        spec = spec_from_file_location('operator_ai_test_renderer', root / 'scripts/profiles/render.py')
        renderer = module_from_spec(spec)
        spec.loader.exec_module(renderer)
        return renderer.resolve('self_hosted', stage='local')['profiles']['self_hosted.local']


def hosted(name, *, cloudflare=None):
    original = selected()
    return operator_ai.configure(original, name, cloudflare=cloudflare)


def test_three_hosted_vendors_are_frozen_and_dimension_true():
    assert set(operator_ai.FROZEN) == {'openrouter', 'siliconflow'}
    assert operator_ai.CLOUDFLARE_GATEWAY == 'cloudflare-gateway'
    for vendor, spec in (*operator_ai.FROZEN.items(), (operator_ai.CLOUDFLARE_GATEWAY, None)):
        if spec is None:
            spec = operator_ai.cloudflare_spec({'account_id': 'a' * 32, 'gateway_id': 'prod'})
        assert spec.embedding_dimension == 1024
        assert spec.model and spec.asr_model and spec.tts_model
        # The Cloudflare /ai/run envelope has no voice concept; the standard
        # vendors must name one frozen voice.
        if spec.provider != operator_ai.CLOUDFLARE_GATEWAY:
            assert spec.tts_voice
        for url in (spec.base_url, spec.embedding_base_url, spec.asr_base_url, spec.tts_base_url):
            assert url.startswith('https://')
    assert operator_ai.FROZEN['openrouter'].embedding_base_url != operator_ai.FROZEN['siliconflow'].embedding_base_url


def test_mimo_selection_is_unchanged_byte_for_byte():
    original = selected()
    row = operator_ai.configure(original, 'mimo-cn')
    assert row['embedding'] == original['embedding']
    assert row['operator_ai'] == asdict(operator_ai.MiMo())
    assert 'llm' not in row and 'speech' not in row
    capabilities.validate(row)
    assert operator_ai.select(row) == operator_ai.MiMo()


@pytest.mark.parametrize('name', ['openrouter', 'siliconflow', 'cloudflare-gateway'])
def test_hosted_selection_owns_every_capability(name):
    original = selected()
    row = operator_ai.configure(original, name, cloudflare={'account_id': 'a' * 32, 'gateway_id': 'prod'})
    assert row['operator_ai']['provider'] == name
    for key in ('llm', 'speech', 'embedding'):
        assert key not in row
    assert row['capabilities']['llm_provider'] == name
    assert row['capabilities']['stt_providers'] == [name]
    assert row['capabilities']['tts_provider'] == name
    assert row['capabilities']['embedding_dims'] == 1024
    assert row['capabilities']['push_provider'] == 'disabled'
    capabilities.validate(row)
    assert operator_ai.select(row).provider == name


def test_cloudflare_gateway_urls_come_from_the_public_manifest_identity():
    spec = operator_ai.cloudflare_spec({'account_id': 'a' * 32, 'gateway_id': 'prod'})
    rest = 'https://api.cloudflare.com/client/v4/accounts/' + 'a' * 32 + '/ai/v1'
    run_base = 'https://api.cloudflare.com/client/v4/accounts/' + 'a' * 32 + '/ai'
    assert spec.base_url == rest
    assert spec.embedding_base_url == rest
    assert spec.asr_base_url == run_base
    assert spec.tts_base_url == run_base
    assert spec.account_id == 'a' * 32 and spec.gateway_id == 'prod'
    assert spec.model == '@cf/meta/llama-3.1-8b-instruct-fast'
    assert spec.embedding_model == '@cf/baai/bge-m3'
    assert spec.embedding_dimension == 1024
    for bad in (
        {},
        {'account_id': 'nothex', 'gateway_id': 'prod'},
        {'account_id': 'A' * 32, 'gateway_id': 'prod'},
        {'account_id': 'a' * 32},
    ):
        with pytest.raises(ValueError):
            operator_ai.cloudflare_spec(bad)
    with pytest.raises(ValueError):
        operator_ai.configure(selected(), 'cloudflare-gateway')
    with pytest.raises(ValueError):
        operator_ai.configure(selected(), 'cloudflare-gateway', cloudflare={'account_id': 'zz', 'gateway_id': 'prod'})


@pytest.mark.parametrize(
    'mutation',
    [
        lambda row: {**row, 'target': 'cloudflare'},
        lambda row: {**row, 'stage': 'legacy'},
        lambda row: {**row, 'llm': row.get('llm') or {'provider': 'ollama'}},
        lambda row: {**row, 'speech': row.get('speech') or {}},
        lambda row: {**row, 'operator_ai': {'vendor': 'nope'}},
        lambda row: {**row, 'operator_ai': {**row['operator_ai'], 'model': 'evil-model'}},
        lambda row: {**row, 'operator_ai': {**row['operator_ai'], 'base_url': 'https://evil.example/v1'}},
        lambda row: {**row, 'operator_ai': 'mimo-cn'},
    ],
)
def test_select_rejects_tampered_hosted_rows(mutation):
    row = operator_ai.configure(selected(), 'openrouter')
    with pytest.raises(ValueError):
        operator_ai.select(mutation(row))


def test_mimo_selection_still_accepts_a_minimal_hand_built_row():
    # Existing consumers build MiMo rows without re-embedding the local model
    # contract; select() must keep admitting them unchanged.
    row = operator_ai.configure(selected(), 'mimo-cn')
    minimal = {key: value for key, value in row.items() if key != 'embedding'}
    assert operator_ai.select(minimal) == operator_ai.MiMo()


def test_hosted_rejects_a_leftover_local_embedding_row():
    row = operator_ai.configure(selected(), 'openrouter')
    row['embedding'] = selected()['embedding']
    with pytest.raises(ValueError):
        operator_ai.select(row)


@pytest.mark.parametrize(
    'vendor,env',
    [
        ('openrouter', 'OPENROUTER_API_KEY'),
        ('siliconflow', 'SILICONFLOW_API_KEY'),
        ('cloudflare-gateway', 'CLOUDFLARE_API_TOKEN'),
    ],
)
def test_per_vendor_credential_envs_are_required(monkeypatch, vendor, env):
    row = operator_ai.configure(selected(), vendor, cloudflare={'account_id': 'a' * 32, 'gateway_id': 'prod'})
    monkeypatch.setattr(profile, 'current', lambda: row)
    for value in (None, '  ', 'spaced key'):
        if value is None:
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, value)
        with pytest.raises(ValueError):
            operator_ai.credentials()
    monkeypatch.setenv(env, 'operator-key')
    assert operator_ai.credentials() == 'operator-key'


def test_cloudflare_gateway_runs_on_one_cloudflare_token(monkeypatch):
    row = operator_ai.configure(
        selected(), 'cloudflare-gateway', cloudflare={'account_id': 'a' * 32, 'gateway_id': 'prod'}
    )
    monkeypatch.setattr(profile, 'current', lambda: row)
    with pytest.raises(ValueError):
        operator_ai.credentials()
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', 'cf-token')
    assert operator_ai.credentials() == 'cf-token'
    assert operator_ai.embedding_credentials() == 'cf-token'
    assert operator_ai.gateway_headers() == {'cf-aig-gateway-id': 'prod'}
    other = operator_ai.configure(selected(), 'openrouter')
    monkeypatch.setattr(profile, 'current', lambda: other)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')
    assert operator_ai.embedding_credentials() == 'or-key'
    assert operator_ai.gateway_headers() == {}


def test_mimo_secret_file_contract_is_unchanged(monkeypatch, tmp_path):
    row = operator_ai.configure(selected(), 'mimo-cn')
    monkeypatch.setattr(profile, 'current', lambda: row)
    with pytest.raises(ValueError):
        operator_ai.credentials()
    secret = tmp_path / 'mimo.json'
    secret.write_text(json.dumps({'MIMO_BASE_URL': operator_ai.MiMo().base_url, 'MIMO_API_KEY': 'stored'}))
    monkeypatch.setenv('MIMO_SECRET_FILE', str(secret))
    assert operator_ai.credentials() == 'stored'
    monkeypatch.setenv('MIMO_API_KEY', 'stored')
    assert operator_ai.credentials() == 'stored'
    monkeypatch.setenv('MIMO_API_KEY', 'other')
    with pytest.raises(ValueError):
        operator_ai.credentials()
    secret.write_text(json.dumps({'MIMO_BASE_URL': 'https://elsewhere/v1'}))
    with pytest.raises(ValueError):
        operator_ai.credentials()


def test_grants_are_exact_endpoints_per_capability(monkeypatch):
    from fork.egress_policy import assert_http_endpoint_allowed, EgressPolicyUnavailable

    row = operator_ai.configure(selected(), 'openrouter')
    spec = operator_ai.select(row)
    grants = {
        spec.base_url + '/chat/completions',
        spec.embedding_base_url + '/embeddings',
        spec.asr_base_url + '/audio/transcriptions',
        spec.tts_base_url + '/audio/speech',
    }
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    monkeypatch.setattr(profile, 'current', lambda: row)
    for url in grants:
        assert assert_http_endpoint_allowed(url) == 'openrouter.ai'
        assert operator_ai.allows(url)
    for url in (
        spec.base_url + '/models',
        spec.embedding_base_url + '/embeddings?x=1',
        'https://openrouter.ai/api/v2/embeddings',
    ):
        assert not operator_ai.allows(url)
        with pytest.raises(EgressPolicyUnavailable):
            assert_http_endpoint_allowed(url)
    mimo_row = operator_ai.configure(selected(), 'mimo-cn')
    monkeypatch.setattr(profile, 'current', lambda: mimo_row)
    mimo_endpoint = operator_ai.MiMo().base_url + '/chat/completions'
    assert operator_ai.allows(mimo_endpoint)
    assert not operator_ai.allows(operator_ai.MiMo().base_url + '/embeddings')
    with pytest.raises(EgressPolicyUnavailable):
        assert_http_endpoint_allowed('https://openrouter.ai/api/v1/embeddings')


def test_no_selection_denies_hosted_vendor_hosts(monkeypatch):
    from fork.egress_policy import assert_http_endpoint_allowed, EgressPolicyUnavailable

    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    monkeypatch.setattr(profile, 'current', lambda: selected())
    for host in ('openrouter.ai', 'api.siliconflow.cn', 'gateway.ai.cloudflare.com'):
        with pytest.raises(EgressPolicyUnavailable):
            assert_http_endpoint_allowed('https://' + host + '/anything')


def test_hosted_chat_build_uses_the_frozen_identity(monkeypatch):
    row = operator_ai.configure(selected(), 'openrouter')
    monkeypatch.setattr(local_llm, 'current', lambda: row)
    monkeypatch.setattr(profile, 'current', lambda: row)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')
    spec = operator_ai.select(row)
    assert local_llm.route('chat_responses') == (spec.model, 'openrouter')
    instance = local_llm.build(*local_llm.route('chat_responses'))
    assert instance.model_name == spec.model
    with pytest.raises(local_llm.LLMInputRejected):
        local_llm.build('other-model', 'openrouter')
    monkeypatch.setattr(local_llm, 'current', lambda: selected())
    monkeypatch.setattr(profile, 'current', lambda: selected())
    with pytest.raises(ValueError):
        local_llm.build(spec.model, 'openrouter')


def test_hosted_embeddings_own_the_dimension_contract(monkeypatch):
    row = operator_ai.configure(selected(), 'openrouter')
    spec = operator_ai.select(row)
    monkeypatch.setattr(profile, 'current', lambda: row)
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')
    vector = [0.5] * spec.embedding_dimension

    def handler(request):
        payload = json.loads(request.content)
        assert request.url.host == 'openrouter.ai'
        assert payload['model'] == spec.embedding_model
        assert payload['input'] == ['hello world']
        body = json.dumps({'data': [{'index': 0, 'embedding': vector}], 'model': spec.embedding_model}).encode()
        import httpx as httpx_module

        return httpx_module.Response(200, content=body, headers={'content-type': 'application/json'})

    import httpx

    instance = embedding.HostedEmbeddings(spec, transport=httpx.MockTransport(handler), timeout=60)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')
    assert instance.embed_query('hello world') == vector
    with pytest.raises(ValueError):
        instance.embed_documents(['a' * 2_000_000])
    assert instance.embed_documents([]) == []

    def wrong_model(request):
        body = json.dumps({'data': [{'index': 0, 'embedding': vector}], 'model': 'totally-unrelated'}).encode()
        return httpx.Response(200, content=body)

    with pytest.raises(embedding.EmbeddingUnavailable):
        embedding.HostedEmbeddings(spec, transport=httpx.MockTransport(wrong_model), timeout=60).embed_query('hello')

    def routed_echo(request):
        # Routed vendors serve bge-m3 under the provider's own id.
        body = json.dumps({'data': [{'index': 0, 'embedding': vector}], 'model': 'parasail-bge-m3'}).encode()
        return httpx.Response(200, content=body)

    routed = embedding.HostedEmbeddings(spec, transport=httpx.MockTransport(routed_echo), timeout=60)
    assert routed.embed_query('hello') == vector

    def wrong_dimension(request):
        body = json.dumps({'data': [{'index': 0, 'embedding': [0.5] * 1536}], 'model': spec.embedding_model}).encode()
        return httpx.Response(200, content=body)

    with pytest.raises(embedding.EmbeddingUnavailable):
        embedding.HostedEmbeddings(spec, transport=httpx.MockTransport(wrong_dimension), timeout=60).embed_query(
            'hello'
        )


def test_build_dispatches_hosted_and_local_owners(monkeypatch):
    hosted = operator_ai.configure(selected(), 'openrouter')
    monkeypatch.setattr(embedding, 'current', lambda: hosted)
    monkeypatch.setattr(embedding.HostedEmbeddings, 'check', lambda self: self)
    monkeypatch.setattr(profile, 'current', lambda: hosted)
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    assert isinstance(embedding.build(), embedding.HostedEmbeddings)
    monkeypatch.setattr(embedding.OllamaEmbeddings, 'check', lambda self: self)
    monkeypatch.setenv('EMBEDDING_ENDPOINT', 'http://embedding:11434')
    original = selected()
    monkeypatch.setattr(embedding, 'current', lambda: original)
    monkeypatch.setattr(profile, 'current', lambda: original)
    assert isinstance(embedding.build(), embedding.OllamaEmbeddings)


def test_hosted_speech_uses_the_shared_windowed_socket(monkeypatch):
    row = operator_ai.configure(selected(), 'openrouter')
    monkeypatch.setattr(profile, 'current', lambda: row)
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')
    assert speech.streaming_service() == 'openrouter'
    assert speech.prerecorded_selection('zh-CN') == ('openrouter', 'zh', 'openai/whisper-large-v3')
    assert speech.streaming_selection('en', exclude={'openrouter'}) == (None, None, None)
    monkeypatch.setattr(profile, 'current', lambda: selected())
    assert speech.streaming_service() == 'sensevoice'


def test_known_providers_union_admits_the_hosted_vendor_names():
    from fork.patches.speech import patches as speech_patches

    names = {'sensevoice', 'mimo'} | set(operator_ai.HOSTED_VENDORS)
    outcome = next(patch for patch in speech_patches() if patch.name.endswith('utils.stt.outcomes._KNOWN_PROVIDERS'))
    assert outcome.build({'sensevoice', 'mimo'}) == names
