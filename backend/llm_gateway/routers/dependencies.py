from __future__ import annotations

from functools import lru_cache

from llm_gateway.gateway.config_loader import GatewayConfig, load_gateway_config
from llm_gateway.gateway.executor import ProviderRegistry
from llm_gateway.gateway.providers import (
    AnthropicMessagesProvider,
    OpenAICompatibleChatCompletionProvider,
    VertexGeminiProvider,
)
from utils.llm.providers import get_openai_compatible_provider_config


def _openai_compatible_provider(name: str) -> OpenAICompatibleChatCompletionProvider:
    config = get_openai_compatible_provider_config(name)
    return OpenAICompatibleChatCompletionProvider(
        api_key_env=config.api_key_env,
        base_url=config.base_url,
        base_url_env=config.base_url_env,
        require_base_url=config.require_base_url,
        default_headers=config.default_headers,
    )


@lru_cache(maxsize=1)
def get_gateway_config() -> GatewayConfig:
    return load_gateway_config(prod_mode=True)


@lru_cache(maxsize=1)
def get_provider_registry() -> ProviderRegistry:
    return ProviderRegistry(
        {
            'openai': _openai_compatible_provider('openai'),
            'openrouter': _openai_compatible_provider('openrouter'),
            'generic': _openai_compatible_provider('generic'),
            'deepseek': _openai_compatible_provider('deepseek'),
            'mimo': _openai_compatible_provider('mimo'),
            'perplexity': OpenAICompatibleChatCompletionProvider(
                api_key_env='PERPLEXITY_API_KEY',
                base_url='https://api.perplexity.ai',
            ),
            'gemini': VertexGeminiProvider(),
            'anthropic': AnthropicMessagesProvider(),
        }
    )


async def close_provider_registry() -> None:
    await get_provider_registry().aclose()
    get_provider_registry.cache_clear()
