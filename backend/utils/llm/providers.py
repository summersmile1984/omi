"""Provider-specific chat model construction for LLM feature routing.

This module owns the mechanics of turning a resolved provider/model route into a
LangChain ``BaseChatModel``. Keep product features out of this file: callers
should route by feature through ``utils.llm.clients.get_llm()`` and let the model
configuration decide which provider/model to use.
"""

import logging
import os
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from utils.llm.gateway_client import GatewayContextChatOpenAI, get_llm_gateway_base_url, get_llm_gateway_service_token
from utils.llm.gateway_resilience import gateway_transport_timeout
from utils.llm.usage_tracker import get_usage_callback

logger = logging.getLogger(__name__)

_usage_callback = get_usage_callback()

# Google's OpenAI-compatible endpoint — used only for BYOK users who bring their
# own AI Studio API key. Platform Gemini calls use ChatGoogleGenerativeAI.
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class ModelProviderConfigurationError(ValueError):
    """A selected direct provider cannot execute with the current deployment config."""

    def __init__(self, provider: str, reason: str, *, setting: str | None = None) -> None:
        self.provider = provider
        self.reason = reason
        self.setting = setting
        detail = f': {setting} is required' if setting else ''
        super().__init__(f"provider {provider!r} is not configured ({reason}){detail}")


@dataclass(frozen=True)
class OpenAICompatibleProviderConfig:
    """Configuration for providers served through ChatOpenAI-compatible APIs."""

    name: str
    api_key_env: str
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None
    require_base_url: bool = False
    default_headers: Dict[str, str] = field(default_factory=dict)
    prefix_google_models: bool = False

    def resolved_base_url(self, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
        values = os.environ if env is None else env
        configured = values.get(self.base_url_env, '').strip() if self.base_url_env else ''
        resolved = configured or (self.base_url or '')
        if self.require_base_url and not resolved:
            raise ValueError(f'{self.base_url_env} is required for provider {self.name!r}')
        return resolved.rstrip('/') or None


OPENAI_COMPATIBLE_PROVIDERS: Dict[str, OpenAICompatibleProviderConfig] = {
    'openai': OpenAICompatibleProviderConfig(
        name='openai',
        api_key_env='OPENAI_API_KEY',
        base_url='https://api.openai.com/v1',
        base_url_env='OPENAI_BASE_URL',
    ),
    'openrouter': OpenAICompatibleProviderConfig(
        name='openrouter',
        api_key_env='OPENROUTER_API_KEY',
        base_url="https://openrouter.ai/api/v1",
        base_url_env='OPENROUTER_BASE_URL',
        default_headers={"X-Title": "Omi Chat"},
        prefix_google_models=True,
    ),
    # Arbitrary OpenAI-compatible service. There is deliberately no vendor
    # default: a missing base URL fails closed instead of reaching OpenAI.
    'generic': OpenAICompatibleProviderConfig(
        name='generic',
        api_key_env='GENERIC_OPENAI_API_KEY',
        base_url_env='GENERIC_OPENAI_BASE_URL',
        require_base_url=True,
    ),
    # Xiaomi MiMo (OpenAI-compatible)
    'mimo': OpenAICompatibleProviderConfig(
        name='mimo',
        api_key_env='MIMO_API_KEY',
        base_url="https://token-plan-cn.xiaomimimo.com/v1",
        base_url_env='MIMO_LLM_BASE_URL',
    ),
    # DeepSeek (OpenAI-compatible)
    'deepseek': OpenAICompatibleProviderConfig(
        name='deepseek',
        api_key_env='DEEPSEEK_API_KEY',
        base_url="https://api.deepseek.com/v1",
        base_url_env='DEEPSEEK_BASE_URL',
    ),
}

_llm_cache: Dict[tuple, Any] = {}


def get_openai_api_key() -> str:
    """Return the platform OpenAI credential at the provider boundary."""

    return os.environ.get(OPENAI_COMPATIBLE_PROVIDERS['openai'].api_key_env, '').strip()


def get_openai_compatible_provider_config(provider: str) -> OpenAICompatibleProviderConfig:
    normalized = provider.strip().lower()
    try:
        return OPENAI_COMPATIBLE_PROVIDERS[normalized]
    except KeyError as exc:
        raise ModelProviderConfigurationError(normalized or provider, 'direct_transport_not_supported') from exc


def _cache_key(provider: str, model_name: str, streaming: bool, options: Dict[str, Any]) -> tuple:
    option_items = tuple(sorted((key, repr(value)) for key, value in options.items()))
    return provider, model_name, streaming, option_items


def openai_compatible_api_model_name(provider_config: OpenAICompatibleProviderConfig, model_name: str) -> str:
    if provider_config.prefix_google_models and model_name.startswith('gemini'):
        return f'google/{model_name}'
    return model_name


def get_or_create_openai_compatible_llm(
    provider: str,
    model_name: str,
    streaming: bool = False,
    options: Optional[Dict[str, Any]] = None,
) -> ChatOpenAI:
    """Get or create a cached ChatOpenAI-compatible chat model."""

    options = options or {}
    provider_config = get_openai_compatible_provider_config(provider)
    # Only include options that are actually transferred to kwargs in the cache key,
    # so arbitrary caller options don't create duplicate cache entries.
    _handled_options = {
        'request_timeout',
        'max_retries',
        'temperature',
        'extra_body',
        'base_url',
        'default_headers',
        'api_key',
    }
    _effective_options = {k: v for k, v in options.items() if k in _handled_options}
    effective_api_key = options.get('api_key') or os.environ.get(provider_config.api_key_env, '').strip()
    if not effective_api_key:
        # Validate credentials before resolving a provider's default URL or
        # constructing ChatOpenAI. Besides producing an actionable failure for
        # the selected primary, this prevents optional providers from borrowing
        # OPENAI_API_KEY through the SDK's environment fallback.
        raise ModelProviderConfigurationError(
            provider_config.name,
            'credential_not_configured',
            setting=provider_config.api_key_env,
        )
    try:
        effective_base_url = options.get('base_url') or provider_config.resolved_base_url()
    except ValueError as exc:
        raise ModelProviderConfigurationError(
            provider_config.name,
            'endpoint_not_configured',
            setting=provider_config.base_url_env,
        ) from exc
    cache_options = {
        **_effective_options,
        'base_url': effective_base_url,
        'api_key_fingerprint': (
            hashlib.sha256(str(effective_api_key).encode()).hexdigest() if effective_api_key else 'environment-default'
        ),
    }
    key = _cache_key(provider_config.name, model_name, streaming, cache_options)
    if key not in _llm_cache:
        kwargs: Dict[str, Any] = {
            'callbacks': [_usage_callback],
            # The direct provider is the recovery path. Keep its established
            # deadline/retry budget; only the optional gateway hop is short.
            'request_timeout': options.get('request_timeout', 120),
            'max_retries': options.get('max_retries', 1),
        }
        if effective_api_key:
            kwargs['api_key'] = effective_api_key
        if effective_base_url:
            kwargs['base_url'] = effective_base_url
        if provider_config.default_headers:
            kwargs['default_headers'] = provider_config.default_headers
        if options.get('extra_body'):
            kwargs['extra_body'] = options['extra_body']
        if 'temperature' in options:
            kwargs['temperature'] = options['temperature']
        if streaming:
            kwargs['streaming'] = True
            kwargs['stream_options'] = {"include_usage": True}

        _llm_cache[key] = ChatOpenAI(model=openai_compatible_api_model_name(provider_config, model_name), **kwargs)
    return _llm_cache[key]


def get_or_create_omi_gateway_llm(
    lane_id: str,
    streaming: bool = False,
    options: Optional[Dict[str, Any]] = None,
    *,
    feature: str | None = None,
) -> ChatOpenAI:
    """Get or create a cached LangChain chat model backed by the Omi LLM gateway."""

    options = options or {}
    base_url = f'{get_llm_gateway_base_url()}/v1'
    request_timeout = options.get('request_timeout', gateway_transport_timeout())
    max_retries = options.get('max_retries', 0)
    service_token = get_llm_gateway_service_token()
    default_headers = {'X-Omi-Service-Caller': 'backend'}
    if service_token:
        default_headers['Authorization'] = f'Bearer {service_token}'
    service_token_cache_key = hashlib.sha256(service_token.encode()).hexdigest() if service_token else 'none'

    key = _cache_key(
        'omi_gateway',
        lane_id,
        streaming,
        {
            'base_url': base_url,
            'service_token': service_token_cache_key,
            'request_timeout': repr(request_timeout),
            'max_retries': max_retries,
            'feature': feature,
        },
    )
    if key not in _llm_cache:
        kwargs: Dict[str, Any] = {
            'api_key': SecretStr('omi-gateway'),
            'base_url': base_url,
            'callbacks': [_usage_callback],
            'default_headers': default_headers,
            'request_timeout': request_timeout,
            'max_retries': max_retries,
        }
        if streaming:
            kwargs['streaming'] = True
            kwargs['stream_options'] = {"include_usage": True}
        _llm_cache[key] = GatewayContextChatOpenAI(model=lane_id, omi_gateway_feature=feature, **kwargs)
    return _llm_cache[key]


def get_or_create_gemini_llm(
    model_name: str, streaming: bool = False, thinking_budget: Optional[int] = None
) -> BaseChatModel:
    """Get or create a cached ChatGoogleGenerativeAI for a Gemini model via native SDK.

    Routing priority:
      1. USE_VERTEX_AI=true + GOOGLE_CLOUD_PROJECT → Vertex AI
      2. GEMINI_API_KEY set → AI Studio
      3. Neither → fail before an SDK client or official URL is constructed

    BYOK users still go through the OpenAI-compatible Gemini endpoint in clients.py.
    """

    use_vertex = os.environ.get('USE_VERTEX_AI', '').strip().lower() == 'true'
    gcp_project = os.environ.get('GOOGLE_CLOUD_PROJECT', '').strip() if use_vertex else ''
    gemini_key = os.environ.get('GEMINI_API_KEY', '').strip()
    if not gcp_project and not gemini_key:
        raise ModelProviderConfigurationError(
            'gemini',
            'credential_not_configured',
            setting='GEMINI_API_KEY or USE_VERTEX_AI=true with GOOGLE_CLOUD_PROJECT',
        )

    auth_identity = (
        f'vertex:{gcp_project}:{os.environ.get("GCP_LOCATION", "us-central1").strip()}'
        if gcp_project
        else f'ai-studio:{hashlib.sha256(gemini_key.encode()).hexdigest()}'
    )
    cache_budget = thinking_budget
    key = (model_name, streaming, 'gemini', auth_identity, cache_budget)
    if key not in _llm_cache:
        kwargs: Dict[str, Any] = {'callbacks': [_usage_callback], 'timeout': 120, 'max_retries': 1}
        if streaming:
            kwargs['streaming'] = True
        if thinking_budget is not None and model_name.startswith('gemini-2.5'):
            kwargs['thinking_budget'] = thinking_budget

        if gcp_project:
            gcp_location = os.environ.get('GCP_LOCATION', 'us-central1')
            _llm_cache[key] = ChatGoogleGenerativeAI(
                model=model_name, project=gcp_project, location=gcp_location, **kwargs
            )
        elif gemini_key:
            kwargs['google_api_key'] = gemini_key
            _llm_cache[key] = ChatGoogleGenerativeAI(model=model_name, **kwargs)
    return _llm_cache[key]


def get_default_client(
    model: str,
    provider: str,
    streaming: bool,
    options: Optional[Dict[str, Any]] = None,
) -> BaseChatModel:
    """Get the cached default client for a model/provider combo."""

    options = options or {}
    if provider == 'gemini':
        return get_or_create_gemini_llm(model, streaming, thinking_budget=options.get('thinking_budget'))
    return get_or_create_openai_compatible_llm(provider, model, streaming, options)
