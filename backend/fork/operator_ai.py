"""Explicit hosted AI selection frozen into Server profiles; no credentials.

The operator picks one hosted vendor per stage (`render.py --operator-ai`);
the vendor's endpoints, model ids and the embedding dimension are frozen here
exactly like MiMo's defaults are, so changing them is a reviewed code change,
never a profile-table edit. Credentials never enter the profile: each vendor
names its own environment variable, and the runtime fails closed without it.
"""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class MiMo:
    provider: str = 'mimo'
    model: str = 'mimo-v2.5'
    asr_model: str = 'mimo-v2.5-asr'
    tts_model: str = 'mimo-v2.5-tts'
    base_url: str = 'https://token-plan-cn.xiaomimimo.com/v1'
    request_timeout_seconds: int = 120
    max_output_tokens: int = 8192


@dataclass(frozen=True)
class HostedOperatorAI:
    """One OpenAI-compatible vendor owning every hosted capability.

    All four capabilities share the vendor's documented OpenAI-compatible wire
    shape (`/chat/completions`, `/embeddings`, `/audio/transcriptions`,
    `/audio/speech`). Per-capability origins exist because a gateway fronts a
    different upstream per capability. No vendor fallback is expressible.
    """

    provider: str
    base_url: str
    model: str
    embedding_base_url: str
    embedding_model: str
    embedding_dimension: int
    asr_base_url: str
    asr_model: str
    tts_base_url: str
    tts_model: str
    tts_voice: str
    tts_response_format: str
    request_timeout_seconds: int = 120
    max_output_tokens: int = 8192
    account_id: str = ''
    gateway_id: str = ''


# Verified against each vendor's public catalog on 2026-09-16, then exercised
# from this deployment's network: the OpenAI and Google upstreams 403 on the
# operator's region, so the OpenRouter chat pick is Alibaba's qwen3-8b and TTS
# is MiniMax with its documented voice id (the dated OpenAI TTS slugs no longer
# exist there). bge-m3 keeps the profile's 1024-dimension invariant everywhere,
# so the Qdrant collections and the profile embedding contract stay untouched.
_OPENROUTER = HostedOperatorAI(
    provider='openrouter',
    base_url='https://openrouter.ai/api/v1',
    model='qwen/qwen3-8b',
    embedding_base_url='https://openrouter.ai/api/v1',
    embedding_model='baai/bge-m3',
    embedding_dimension=1024,
    asr_base_url='https://openrouter.ai/api/v1',
    asr_model='openai/whisper-large-v3',
    tts_base_url='https://openrouter.ai/api/v1',
    tts_model='minimax/speech-2.8-turbo',
    tts_voice='female-shaonv',
    tts_response_format='mp3',
)

_SILICONFLOW = HostedOperatorAI(
    provider='siliconflow',
    base_url='https://api.siliconflow.cn/v1',
    model='Qwen/Qwen3-32B',
    embedding_base_url='https://api.siliconflow.cn/v1',
    embedding_model='BAAI/bge-m3',
    embedding_dimension=1024,
    asr_base_url='https://api.siliconflow.cn/v1',
    asr_model='FunAudioLLM/SenseVoiceSmall',
    tts_base_url='https://api.siliconflow.cn/v1',
    tts_model='FunAudioLLM/CosyVoice2-0.5B',
    tts_voice='FunAudioLLM/CosyVoice2-0.5B:anna',
    tts_response_format='mp3',
)

# Code-frozen vendors: their entire spec, including URLs, is reviewed source.
FROZEN = {'openrouter': _OPENROUTER, 'siliconflow': _SILICONFLOW}

# cloudflare-gateway is manifest-derived: the account-scoped REST base URL
# needs the operator's public account/gateway identity from the brand
# manifest, the same place their domains live. 2026-09-16 dashboard: the
# account-scoped REST API is the current surface — one CLOUDFLARE_API_TOKEN
# authorizes every capability, `cf-aig-gateway-id` routes through the
# operator's gateway, and the four capabilities stay on the production-
# verified Workers AI models the fork's Cloudflare deployment already runs.
CLOUDFLARE_GATEWAY = 'cloudflare-gateway'
HOSTED_VENDORS = frozenset(FROZEN) | {CLOUDFLARE_GATEWAY}

# The main provider key per vendor. MiMo keeps its historical variable.
CREDENTIAL_ENV = {
    'mimo': 'MIMO_API_KEY',
    'openrouter': 'OPENROUTER_API_KEY',
    'cloudflare-gateway': 'CLOUDFLARE_API_TOKEN',
    'siliconflow': 'SILICONFLOW_API_KEY',
}
CLOUDFLARE_TOKEN_ENV = 'CLOUDFLARE_API_TOKEN'


def cloudflare_spec(gateway):
    """Build the Cloudflare AI Gateway spec from the brand manifest's public ids."""
    if not isinstance(gateway, dict):
        raise ValueError('cloudflare-gateway requires cloudflare_ai_gateway in the brand manifest')
    account = gateway.get('account_id', '')
    name = gateway.get('gateway_id', '')
    if (
        not isinstance(account, str)
        or not isinstance(name, str)
        or not re.fullmatch(r'[0-9a-f]{32}', account)
        or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', name)
    ):
        raise ValueError('cloudflare_ai_gateway needs a 32-hex account_id and a gateway_id slug')
    rest = 'https://api.cloudflare.com/client/v4/accounts/' + account + '/ai/v1'
    run = 'https://api.cloudflare.com/client/v4/accounts/' + account + '/ai/run'
    return HostedOperatorAI(
        provider='cloudflare-gateway',
        # Chat and embeddings share the account's OpenAI-compatible REST base;
        # ASR and TTS use the universal /ai/run envelope, whose shapes are the
        # same Workers AI models the fork's Cloudflare deployment runs today.
        base_url=rest,
        model='@cf/meta/llama-3.1-8b-instruct-fast',
        embedding_base_url=rest,
        embedding_model='@cf/baai/bge-m3',
        embedding_dimension=1024,
        asr_base_url=run,
        asr_model='@cf/openai/whisper-large-v3-turbo',
        tts_base_url=run,
        tts_model='@cf/deepgram/aura-1',
        tts_voice='',
        tts_response_format='wav',
        account_id=account,
        gateway_id=name,
    )


def _hosted_expected(value):
    if not isinstance(value, dict) or not isinstance(value.get('provider'), str):
        return None
    vendor = value['provider']
    if vendor in FROZEN:
        return asdict(FROZEN[vendor])
    if vendor == CLOUDFLARE_GATEWAY:
        try:
            return asdict(cloudflare_spec(value))
        except ValueError:
            return None
    return None


def select(row):
    value = row.get('operator_ai')
    if value is None:
        return None
    if (
        row.get('target') != 'self_hosted'
        or row.get('stage') not in {'local', 'beta', 'production'}
        or row.get('llm') is not None
        or row.get('speech') is not None
    ):
        raise ValueError('operator AI requires an explicit Server profile without local model rows')
    if value == asdict(MiMo()):
        return MiMo()
    expected = _hosted_expected(value)
    if expected is None or value != expected:
        raise ValueError('hosted operator AI values must match the reviewed frozen selection')
    if row.get('embedding') is not None:
        raise ValueError('a hosted operator AI owns embeddings; no local embedding row may remain')
    if value['provider'] == CLOUDFLARE_GATEWAY:
        return cloudflare_spec(value)
    return FROZEN[value['provider']]


def configure(row, name, *, cloudflare=None):
    if name != 'mimo-cn' and name not in HOSTED_VENDORS:
        raise ValueError('unknown operator AI selection')
    result = dict(row)
    result.pop('llm', None)
    result.pop('speech', None)
    if name == 'mimo-cn':
        result['operator_ai'] = asdict(MiMo())
        result['capabilities'] = {
            **result['capabilities'],
            'llm_provider': 'mimo',
            'stt_providers': ['mimo'],
            'tts_provider': 'mimo',
        }
        select(result)
        return result
    result.pop('embedding', None)
    result['operator_ai'] = asdict(FROZEN[name] if name in FROZEN else cloudflare_spec(cloudflare))
    select(result)
    selected = result['operator_ai']
    provider = selected['provider']
    result['capabilities'] = {
        **result['capabilities'],
        'llm_provider': provider,
        'stt_providers': [provider],
        'tts_provider': provider,
        'embedding_dims': selected['embedding_dimension'],
    }
    return result


def current():
    from .profile import current as profile

    selected = select(profile())
    if selected is None:
        raise ValueError('no hosted AI is selected by this deployment')
    return selected


def _required_env(name):
    key = os.environ.get(name, '').strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError(f'{name} API credential is required')
    return key


def credentials():
    selected = current()
    if selected.provider == 'mimo':
        key = os.environ.get('MIMO_API_KEY', '').strip()
        filename = os.environ.get('MIMO_SECRET_FILE', '')
        if filename:
            try:
                value = json.loads(Path(filename).read_text())
                if value.get('MIMO_BASE_URL') != selected.base_url:
                    raise ValueError('MiMo secret endpoint conflicts with the selected profile')
                stored = value.get('MIMO_API_KEY', '').strip()
                if key and key != stored:
                    raise ValueError('MiMo credential sources conflict')
                key = stored
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError('MiMo secret file is unavailable') from error
        if not key or any(character.isspace() for character in key):
            raise ValueError('MiMo API credential is required')
        return key
    return _required_env(CREDENTIAL_ENV[selected.provider])


def embedding_credentials():
    """The bearer used by the hosted embeddings endpoint.

    Every hosted vendor authenticates embeddings with its one provider key;
    Cloudflare's account REST API is the same authority as its gateway use, so
    the single token covers everything.
    """
    return credentials()


def gateway_headers():
    """Extra vendor routing headers beyond the provider Authorization bearer.

    Cloudflare routes account-scoped REST calls through the operator's gateway
    with this id; the other vendors have no equivalent concept.
    """
    selected = current()
    if selected.provider == CLOUDFLARE_GATEWAY:
        return {'cf-aig-gateway-id': selected.gateway_id}
    return {}


def _grants(spec):
    if isinstance(spec, MiMo):
        return (spec.base_url + '/chat/completions',)
    asr_path = '/run' if spec.provider == CLOUDFLARE_GATEWAY else '/audio/transcriptions'
    tts_path = '/run' if spec.provider == CLOUDFLARE_GATEWAY else '/audio/speech'
    return (
        spec.base_url + '/chat/completions',
        spec.embedding_base_url + '/embeddings',
        spec.asr_base_url + asr_path,
        spec.tts_base_url + tts_path,
    )


def allows(url):
    # This is an exact API endpoint grant, not a vendor-wide host allowlist.
    from .profile import current as profile, ProfileError

    try:
        selected = select(profile())
    except (ValueError, ProfileError):
        return False
    return selected is not None and url in _grants(selected)
