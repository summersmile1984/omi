"""Pure runtime configuration contract for backend translation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from os import environ as process_environ
from typing import Mapping


class TranslationProvider(str, Enum):
    gemini = 'gemini'
    # Internal compatibility alias: this must never produce "google" telemetry.
    google = 'gemini'
    generic = 'generic'
    nllb = 'nllb'
    disabled = 'disabled'

    @staticmethod
    def get_display_name(value: 'TranslationProvider') -> str:
        if value == TranslationProvider.gemini:
            return 'Gemini 2.5 Flash-Lite via LLM gateway'
        if value == TranslationProvider.generic:
            return 'Operator-configured generic LLM'
        if value == TranslationProvider.nllb:
            return 'NLLB-200 (self-hosted)'
        if value == TranslationProvider.disabled:
            return 'Disabled by deployment'
        return str(value)


@dataclass(frozen=True)
class TranslationProfile:
    """Resolved provider and cache policy for one translation call."""

    providers: tuple[TranslationProvider, ...]
    nllb_url: str
    nllb_timeout_seconds: float
    cache_ttl_seconds: int
    negative_cache_ttl_seconds: int
    configured_providers: tuple[TranslationProvider, ...] = ()
    max_batch_size: int = 100
    unsupported_tokens: tuple[str, ...] = ()
    unavailable_tokens: tuple[str, ...] = ()

    @property
    def primary_provider(self) -> TranslationProvider:
        return self.providers[0]


def resolve_translation_profile(env: Mapping[str, str] | None = None) -> TranslationProfile:
    """Resolve mutable environment at the translation call boundary.

    The configured list is an ordered provider policy. Unavailable providers
    are filtered and unsupported tokens are retained as diagnostics. Managed
    deployments preserve the historical Gemini default; self-hosted/neutral
    deployments fail closed unless ``generic``, ``nllb``, or ``disabled`` is
    explicitly selected.
    """

    values = process_environ if env is None else env
    neutral_deployment = values.get('OMI_DEPLOYMENT_PROFILE', '').strip().lower() in {
        'neutral',
        'self_hosted',
        'self-hosted',
    }
    nllb_url = values.get('HOSTED_TRANSLATION_API_URL', '').strip()
    raw_models = values.get('TRANSLATION_SERVICE_MODELS', '').strip()

    configured_providers: list[TranslationProvider] = []
    usable_providers: list[TranslationProvider] = []
    unsupported_tokens: list[str] = []
    unavailable_tokens: list[str] = []
    for raw_token in raw_models.split(',') if raw_models else ():
        token = raw_token.strip().lower()
        if not token:
            continue
        if token in {TranslationProvider.gemini.value, 'google'}:
            if neutral_deployment:
                if token not in unsupported_tokens:
                    unsupported_tokens.append(token)
                continue
            provider = TranslationProvider.gemini
        elif token == TranslationProvider.generic.value:
            provider = TranslationProvider.generic
        elif token == TranslationProvider.nllb.value:
            provider = TranslationProvider.nllb
        elif token == TranslationProvider.disabled.value:
            provider = TranslationProvider.disabled
        else:
            if token not in unsupported_tokens:
                unsupported_tokens.append(token)
            continue
        if provider not in configured_providers:
            configured_providers.append(provider)
        if provider == TranslationProvider.nllb and not nllb_url:
            if token not in unavailable_tokens:
                unavailable_tokens.append(token)
            continue
        if provider not in usable_providers:
            usable_providers.append(provider)

    if TranslationProvider.disabled in usable_providers:
        providers = (TranslationProvider.disabled,)
    elif usable_providers:
        providers = tuple(usable_providers)
    elif neutral_deployment:
        providers = (TranslationProvider.disabled,)
        if not configured_providers and 'translation_not_configured' not in unavailable_tokens:
            unavailable_tokens.append('translation_not_configured')
    else:
        providers = (TranslationProvider.gemini,)

    timeout = _positive_float(values.get('TRANSLATION_NLLB_TIMEOUT_SECONDS', '5.0'), 'TRANSLATION_NLLB_TIMEOUT_SECONDS')
    cache_ttl = _positive_int(values.get('TRANSLATION_CACHE_TTL', str(60 * 60 * 24 * 14)), 'TRANSLATION_CACHE_TTL')
    negative_ttl = _positive_int(
        values.get('TRANSLATION_NEGATIVE_CACHE_TTL', str(60 * 60 * 24 * 7)),
        'TRANSLATION_NEGATIVE_CACHE_TTL',
    )

    return TranslationProfile(
        providers=providers,
        nllb_url=nllb_url,
        nllb_timeout_seconds=timeout,
        cache_ttl_seconds=cache_ttl,
        negative_cache_ttl_seconds=negative_ttl,
        configured_providers=tuple(configured_providers),
        unsupported_tokens=tuple(unsupported_tokens),
        unavailable_tokens=tuple(unavailable_tokens),
    )


def _positive_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{name} must be a number') from error
    if value <= 0:
        raise ValueError(f'{name} must be greater than zero')
    return value


def _positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{name} must be an integer') from error
    if value <= 0:
        raise ValueError(f'{name} must be greater than zero')
    return value
