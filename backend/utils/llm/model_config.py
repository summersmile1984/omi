"""Model/profile configuration for backend LLM feature routing.

This module is the source of truth for feature → (model, provider) routing.
Provider-specific client construction lives in ``providers.py``; callers should
continue to use ``clients.get_llm(feature)``.
"""

import logging
import os
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple, Union

from utils.llm.gateway_route_ids import is_auto_lane_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExplicitRouteRef:
    feature: str
    model: str
    provider: str
    options: Dict[str, object]


@dataclass(frozen=True)
class AutoLaneRouteRef:
    feature: str
    lane_id: str


RouteRef = Union[ExplicitRouteRef, AutoLaneRouteRef]


@dataclass(frozen=True)
class ProviderRoute:
    """One provider/model leg shared by direct and gateway execution."""

    provider: str
    model: str


@dataclass(frozen=True)
class FeatureRouteSpec:
    """Environment contract for one configured model workload."""

    feature: str
    default: ProviderRoute
    provider_env: str
    model_env: str
    fallbacks_env: str


@dataclass(frozen=True)
class ResolvedFeatureRoute:
    """Resolved primary and explicitly configured fallback legs."""

    feature: str
    primary: ProviderRoute
    fallbacks: Tuple[ProviderRoute, ...]
    source: str


# ---------------------------------------------------------------------------
# Model QoS Profile System
#
# Each profile maps every feature to a (model, provider) tuple.
# The profile is the SINGLE SOURCE OF TRUTH for both model and provider.
# Provider is never inferred from model name — it is declared explicitly.
#
# This means the same model can be hosted by different providers:
#   feature_a: ('gemini-2.5-flash', 'gemini')      → Google direct
#   feature_b: ('gemini-2.5-flash', 'openrouter')   → OpenRouter
#
# Global switch:     MODEL_QOS=premium        (selects entire profile)
#
# Profiles:
#   premium  — maximize cost savings while preserving 80% of max quality
#   max      — 100% quality, best models available, no cost optimization
#   byok     — same models as max (BYOK users pay their own API costs)
# ---------------------------------------------------------------------------

# All QoS profiles deliberately share this two-tier map. Keeping independent
# copies below retains profile selection semantics while preventing a higher
# tier or BYOK route from reintroducing a retired OpenAI text model.
_TWO_TIER_MODEL_PROFILE: Dict[str, Tuple[str, str]] = {
    # OpenAI — default intelligence
    'conv_action_items': ('gpt-5.6-luna', 'openai'),
    'conv_structure': ('gpt-5.6-luna', 'openai'),
    'conv_app_result': ('gpt-5.6-luna', 'openai'),
    'daily_summary': ('gpt-5.6-luna', 'openai'),
    'external_structure': ('gpt-5.6-luna', 'openai'),
    'memories': ('gpt-5.6-luna', 'openai'),
    'x_memory_extraction_flex': ('gpt-5.6-luna', 'openai'),
    'learnings': ('gpt-5.6-luna', 'openai'),
    'memory_conflict': ('gpt-5.6-luna', 'openai'),
    'memory_conflict_flex': ('gpt-5.6-luna', 'openai'),
    'knowledge_graph': ('gpt-5.6-luna', 'openai'),
    'memory_l1': ('gpt-5.6-luna', 'openai'),
    'memory_l2': ('gpt-5.6-luna', 'openai'),
    'memory_l2_flex': ('gpt-5.6-luna', 'openai'),
    'chat_responses': ('gpt-5.6-luna', 'openai'),
    'chat_extraction': ('gpt-5.6-luna', 'openai'),
    'chat_graph': ('gpt-5.6-luna', 'openai'),
    'goals': ('gpt-5.6-luna', 'openai'),
    'goals_advice': ('gpt-5.6-luna', 'openai'),
    'notifications': ('gpt-5.6-luna', 'openai'),
    'proactive_notification': ('gpt-5.6-luna', 'openai'),
    'desktop_proactive_reasoning': ('gpt-5.6-luna', 'openai'),
    'what_matters_now': ('gpt-5.6-luna', 'openai'),
    'openglass': ('gpt-5.6-luna', 'openai'),
    'app_generator': ('gpt-5.6-luna', 'openai'),
    'persona_clone': ('gpt-5.6-luna', 'openai'),
    'persona_chat_premium': ('gpt-5.6-luna', 'openai'),
    # OpenAI — cheapest light/binary work
    'public_shared_conversation_chat': ('gpt-5-nano', 'openai'),
    'conv_app_select': ('gpt-5-nano', 'openai'),
    'conv_folder': ('gpt-5-nano', 'openai'),
    'conv_discard': ('gpt-5-nano', 'openai'),
    'daily_summary_simple': ('gpt-5-nano', 'openai'),
    'memory_category': ('gpt-5-nano', 'openai'),
    'smart_glasses': ('gpt-5-nano', 'openai'),
    'persona_chat': ('gpt-5-nano', 'openai'),
    'desktop_proactive_extraction': ('gpt-5-nano', 'openai'),
    # Non-OpenAI routes remain intentionally unchanged.
    'session_titles': ('gemini-2.5-flash-lite', 'gemini'),
    'followup': ('gemini-2.5-flash-lite', 'gemini'),
    'onboarding': ('gemini-2.5-flash-lite', 'gemini'),
    'app_integration': ('gemini-2.5-flash-lite', 'gemini'),
    'trends': ('gemini-2.5-flash-lite', 'gemini'),
    'translation': ('gemini-2.5-flash-lite', 'gemini'),
    'chat_agent': ('claude-sonnet-4-6', 'anthropic'),
    'wrapped_analysis': ('gemini-3-flash-preview', 'openrouter'),
    'web_search': ('sonar-pro', 'perplexity'),
}

MODEL_QOS_PROFILES: Dict[str, Dict[str, Tuple[str, str]]] = {
    profile_name: dict(_TWO_TIER_MODEL_PROFILE) for profile_name in ('premium', 'max', 'byok')
}

# Pinned features — (model, provider) fixed regardless of profile or env override.
_PINNED_FEATURES: Dict[str, Tuple[str, str]] = {
    'fair_use': (os.getenv('FAIR_USE_CLASSIFIER_MODEL', 'gpt-5.6-luna').strip() or 'gpt-5.6-luna', 'openai'),
}

# Resolve active profile once at startup.
_active_profile_name = os.environ.get('MODEL_QOS', 'premium').strip().lower()
if _active_profile_name not in MODEL_QOS_PROFILES:
    logger.warning('MODEL_QOS=%s is not a valid profile, falling back to premium', _active_profile_name)
    _active_profile_name = 'premium'
_active_profile = MODEL_QOS_PROFILES[_active_profile_name]

# BYOK QoS — all BYOK users get routed to 'byok' profile (top-tier all-OpenAI).
# BYOK users pay their own API costs, so we give them maximum quality models.
_byok_profile_name = 'byok'
_byok_profile = MODEL_QOS_PROFILES[_byok_profile_name]

FEATURE_ROUTE_ENV_PREFIX = 'OMI_LLM_ROUTE'
DEFAULT_ROUTE_PROVIDER_ENV = 'OMI_LLM_DEFAULT_PROVIDER'
DEFAULT_ROUTE_MODEL_ENV = 'OMI_LLM_DEFAULT_MODEL'
DEFAULT_ROUTE_FALLBACKS_ENV = 'OMI_LLM_DEFAULT_FALLBACKS'

_PROVIDER_ALIASES = {
    'ds': 'deepseek',
    'xiaomi': 'mimo',
    'openai_compatible': 'generic',
    'openai-compatible': 'generic',
}
_PROVIDER_DEFAULT_MODELS: Dict[str, Tuple[str, str]] = {
    'generic': ('GENERIC_OPENAI_MODEL', ''),
    'deepseek': ('DEEPSEEK_MODEL', 'deepseek-chat'),
    'mimo': ('MIMO_LLM_MODEL', 'mimo-v2.5'),
}


def _feature_env_slug(feature: str) -> str:
    return ''.join(character if character.isalnum() else '_' for character in feature).upper()


def _feature_route_spec(feature: str, route: Tuple[str, str]) -> FeatureRouteSpec:
    model, provider = route
    prefix = f'{FEATURE_ROUTE_ENV_PREFIX}_{_feature_env_slug(feature)}'
    return FeatureRouteSpec(
        feature=feature,
        default=ProviderRoute(provider=provider, model=model),
        provider_env=f'{prefix}_PROVIDER',
        model_env=f'{prefix}_MODEL',
        fallbacks_env=f'{prefix}_FALLBACKS',
    )


# Explicit inventory for every configured workload. Adding a model-config feature
# without adding it to this manifest is prevented by the routing-matrix test.
FEATURE_ROUTE_MANIFEST: Dict[str, FeatureRouteSpec] = {
    feature: _feature_route_spec(feature, route) for feature, route in {**_active_profile, **_PINNED_FEATURES}.items()
}

# Features that can't go through get_llm() (non-ChatOpenAI providers).
_ANTHROPIC_ONLY_FEATURES = {'chat_agent'}
_PERPLEXITY_ONLY_FEATURES = {'web_search'}


# Feature-specific client config (temperature, headers — orthogonal to model choice).
# Only applied when a feature resolves to an OpenRouter model.
_OPENROUTER_TEMPERATURES: Dict[str, float] = {
    'wrapped_analysis': 0.7,
}

# Prompt-cache capability detection.
#
# OpenAI prompt caching is a capability of whole model families, not of specific point
# releases. Gating on exact model names silently breaks when a family member changes,
# so we detect by family prefix.
#
#   prompt_cache_key             — prefix-cache request routing. Supported by the gpt-4o,
#                                  gpt-4o, gpt-5.x and o-series families.
#   prompt_cache_retention='24h' — extended (24h) cache retention. Supported by the
#                                  gpt-5.x and o-series families, except gpt-5.6, which
#                                  uses the explicit prompt_cache_options contract instead
#                                  (see supports_cache_retention).
_CACHE_KEY_MODEL_PREFIXES = ('gpt-5', 'gpt-4o', 'o1', 'o3', 'o4')
_CACHE_RETENTION_MODEL_PREFIXES = ('gpt-5', 'o1', 'o3', 'o4')

# Features that call .with_structured_output() — logged when resolving to Gemini for compat monitoring.
_STRUCTURED_OUTPUT_FEATURES = {
    'chat_extraction',
    'proactive_notification',
    'desktop_proactive_extraction',
    'desktop_proactive_reasoning',
    'conv_app_select',
    'external_structure',
    'trends',
    'what_matters_now',
    'translation',
}
STRUCTURED_OUTPUT_FEATURES = _STRUCTURED_OUTPUT_FEATURES

_DEFAULT_CONFIG: Tuple[str, str] = ('gpt-5.6-luna', 'openai')
DEFAULT_CONFIG = _DEFAULT_CONFIG

# Future migration point for features that should call the gateway via an auto
# lane. Keep empty until a ticket explicitly wires and verifies shadow/live
# traffic; existing direct LLM routing never consults this map.
_AUTO_LANE_FEATURES: Dict[str, str] = {}
_CHAT_FEATURES = {'chat_responses', 'chat_extraction', 'chat_graph'}


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _neutral_deployment(values: Mapping[str, str]) -> bool:
    """Identify profiles where checked-in managed model defaults are forbidden."""

    return values.get('OMI_DEPLOYMENT_PROFILE', '').strip().lower() in {
        'neutral',
        'self_hosted',
        'self-hosted',
    }


def _provider_default_model(provider: str, values: Mapping[str, str], *, allow_builtin: bool = True) -> str:
    config = _PROVIDER_DEFAULT_MODELS.get(provider)
    if config is None:
        return ''
    env_name, default = config
    return values.get(env_name, '').strip() or (default if allow_builtin else '')


def _parse_fallbacks(raw: str, *, feature: str) -> Tuple[ProviderRoute, ...]:
    """Parse ``provider:model`` legs from one bounded deployment variable."""

    if not raw.strip():
        return ()
    routes: list[ProviderRoute] = []
    for value in raw.split(','):
        provider, separator, model = value.strip().partition(':')
        provider = _normalize_provider(provider)
        model = model.strip()
        if not separator or not provider or not model:
            raise ValueError(
                f"Invalid fallback route for feature '{feature}': expected comma-separated provider:model values"
            )
        route = ProviderRoute(provider=provider, model=model)
        if route not in routes:
            routes.append(route)
    return tuple(routes)


def _group_route_override(
    feature: str, values: Mapping[str, str], *, neutral_deployment: bool = False
) -> Optional[ProviderRoute]:
    """Keep the intentional translation/chat deployment groups below per-feature routes."""

    if feature == 'translation':
        provider = _normalize_provider(values.get('TRANSLATION_PROVIDER', ''))
        model = values.get('TRANSLATION_MODEL', '').strip()
    elif feature in _CHAT_FEATURES:
        provider = _normalize_provider(values.get('CHAT_PROVIDER', ''))
        model = values.get('CHAT_MODEL', '').strip()
    else:
        return None
    if not provider:
        return None
    if provider not in {'generic', 'mimo', 'deepseek'}:
        return None
    group_default = ''
    if provider == 'deepseek':
        if not neutral_deployment:
            group_default = 'deepseek-chat' if feature == 'translation' else 'deepseek-v4-flash'
    elif provider == 'mimo':
        if not neutral_deployment:
            group_default = 'mimo-v2.5'
    resolved_model = (
        model or group_default or _provider_default_model(provider, values, allow_builtin=not neutral_deployment)
    )
    if not resolved_model:
        if neutral_deployment:
            raise ValueError(f"Feature '{feature}' requires an explicit provider/model route in a neutral deployment")
        raise ValueError(f"Provider '{provider}' for feature '{feature}' requires a configured model")
    return ProviderRoute(provider=provider, model=resolved_model)


def resolve_feature_route(
    feature: str,
    env: Optional[Mapping[str, str]] = None,
) -> ResolvedFeatureRoute:
    """Resolve one workload once for both direct clients and generated gateway lanes.

    Precedence is per-feature env > intentional group env > deployment default >
    checked-in profile. Fallbacks use the per-feature list when present, otherwise
    the deployment-wide list. They never appear implicitly.
    """

    values = os.environ if env is None else env
    spec = FEATURE_ROUTE_MANIFEST.get(feature)
    if spec is None:
        # Unknown legacy callers retain the historical default, but are not part
        # of the gateway/configured-feature inventory.
        model, provider = _DEFAULT_CONFIG
        spec = _feature_route_spec(feature, (model, provider))

    provider_value = values.get(spec.provider_env, '').strip()
    model_value = values.get(spec.model_env, '').strip()
    if provider_value or model_value:
        provider = _normalize_provider(provider_value) or spec.default.provider
        model = model_value or _provider_default_model(provider, values, allow_builtin=not _neutral_deployment(values))
        if not model and provider == spec.default.provider and not _neutral_deployment(values):
            model = spec.default.model
        if not model:
            if _neutral_deployment(values):
                raise ValueError(
                    f"Feature '{feature}' requires an explicit provider/model route in a neutral deployment"
                )
            raise ValueError(f"{spec.model_env} is required when {spec.provider_env} selects '{provider}'")
        primary = ProviderRoute(provider=provider, model=model)
        source = 'feature_env'
    else:
        group_route = _group_route_override(feature, values, neutral_deployment=_neutral_deployment(values))
        if group_route is not None:
            primary = group_route
            source = 'group_env'
        else:
            default_provider_value = values.get(DEFAULT_ROUTE_PROVIDER_ENV, '').strip()
            default_model_value = values.get(DEFAULT_ROUTE_MODEL_ENV, '').strip()
            if default_provider_value or default_model_value:
                provider = _normalize_provider(default_provider_value) or spec.default.provider
                model = default_model_value or _provider_default_model(
                    provider, values, allow_builtin=not _neutral_deployment(values)
                )
                if not model and provider == spec.default.provider and not _neutral_deployment(values):
                    model = spec.default.model
                if not model:
                    if _neutral_deployment(values):
                        raise ValueError(
                            f"Feature '{feature}' requires an explicit provider/model route in a neutral deployment"
                        )
                    raise ValueError(
                        f'{DEFAULT_ROUTE_MODEL_ENV} is required when {DEFAULT_ROUTE_PROVIDER_ENV} selects {provider!r}'
                    )
                primary = ProviderRoute(provider=provider, model=model)
                source = 'default_env'
            else:
                if _neutral_deployment(values):
                    raise ValueError(
                        f"Feature '{feature}' requires an explicit provider/model route in a neutral deployment"
                    )
                primary = spec.default
                source = 'profile'

    fallback_raw = values.get(spec.fallbacks_env, '').strip()
    if not fallback_raw:
        fallback_raw = values.get(DEFAULT_ROUTE_FALLBACKS_ENV, '').strip()
    fallbacks = tuple(route for route in _parse_fallbacks(fallback_raw, feature=feature) if route != primary)
    return ResolvedFeatureRoute(feature=feature, primary=primary, fallbacks=fallbacks, source=source)


# Deployment overrides are deliberately explicit. ``generic`` has no vendor
# default, so an operator must provide both its endpoint and model. These
# variables are consumed by the direct client and by generated gateway lanes,
# keeping model/provider ownership in one place.
FEATURE_ROUTE_ENV_PREFIX = 'OMI_LLM_ROUTE'
DEFAULT_ROUTE_PROVIDER_ENV = 'OMI_LLM_DEFAULT_PROVIDER'
DEFAULT_ROUTE_MODEL_ENV = 'OMI_LLM_DEFAULT_MODEL'
DEFAULT_ROUTE_FALLBACKS_ENV = 'OMI_LLM_DEFAULT_FALLBACKS'
_CHAT_FEATURES = {'chat_responses', 'chat_extraction', 'chat_graph'}
_PROVIDER_ALIASES = {
    'ds': 'deepseek',
    'xiaomi': 'mimo',
    'openai_compatible': 'generic',
    'openai-compatible': 'generic',
}
_PROVIDER_DEFAULT_MODELS: Dict[str, Tuple[str, str]] = {
    'generic': ('GENERIC_OPENAI_MODEL', ''),
    'deepseek': ('DEEPSEEK_MODEL', 'deepseek-chat'),
    'mimo': ('MIMO_LLM_MODEL', 'mimo-v2.5'),
}


def _feature_env_slug(feature: str) -> str:
    return ''.join(character if character.isalnum() else '_' for character in feature).upper()


def _feature_route_spec(feature: str, route: Tuple[str, str]) -> FeatureRouteSpec:
    model, provider = route
    prefix = f'{FEATURE_ROUTE_ENV_PREFIX}_{_feature_env_slug(feature)}'
    return FeatureRouteSpec(
        feature=feature,
        default=ProviderRoute(provider=provider, model=model),
        provider_env=f'{prefix}_PROVIDER',
        model_env=f'{prefix}_MODEL',
        fallbacks_env=f'{prefix}_FALLBACKS',
    )


FEATURE_ROUTE_MANIFEST: Dict[str, FeatureRouteSpec] = {
    feature: _feature_route_spec(feature, route) for feature, route in {**_active_profile, **_PINNED_FEATURES}.items()
}


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _neutral_deployment(values: Mapping[str, str]) -> bool:
    """Return whether checked-in managed vendor routes must be rejected."""

    return values.get('OMI_DEPLOYMENT_PROFILE', '').strip().lower() in {
        'neutral',
        'self_hosted',
        'self-hosted',
    }


def _provider_default_model(provider: str, values: Mapping[str, str], *, allow_builtin: bool = True) -> str:
    config = _PROVIDER_DEFAULT_MODELS.get(provider)
    if config is None:
        return ''
    env_name, default = config
    return values.get(env_name, '').strip() or (default if allow_builtin else '')


def _parse_fallbacks(raw: str, *, feature: str) -> Tuple[ProviderRoute, ...]:
    if not raw.strip():
        return ()
    routes: list[ProviderRoute] = []
    for value in raw.split(','):
        provider, separator, model = value.strip().partition(':')
        provider = _normalize_provider(provider)
        model = model.strip()
        if not separator or not provider or not model:
            raise ValueError(
                f"Invalid fallback route for feature '{feature}': expected comma-separated provider:model values"
            )
        route = ProviderRoute(provider=provider, model=model)
        if route not in routes:
            routes.append(route)
    return tuple(routes)


def _group_route_override(
    feature: str, values: Mapping[str, str], *, neutral_deployment: bool = False
) -> Optional[ProviderRoute]:
    if feature == 'translation':
        provider = _normalize_provider(values.get('TRANSLATION_PROVIDER', ''))
        model = values.get('TRANSLATION_MODEL', '').strip()
    elif feature in _CHAT_FEATURES:
        provider = _normalize_provider(values.get('CHAT_PROVIDER', ''))
        model = values.get('CHAT_MODEL', '').strip()
    else:
        return None
    if not provider:
        return None
    if provider not in {'generic', 'mimo', 'deepseek'}:
        return None
    group_default = ''
    if not neutral_deployment:
        if provider == 'deepseek':
            group_default = 'deepseek-chat' if feature == 'translation' else 'deepseek-v4-flash'
        elif provider == 'mimo':
            group_default = 'mimo-v2.5'
    resolved_model = (
        model or group_default or _provider_default_model(provider, values, allow_builtin=not neutral_deployment)
    )
    if not resolved_model:
        raise ValueError(f"Feature '{feature}' requires an explicit provider/model route in a neutral deployment")
    return ProviderRoute(provider=provider, model=resolved_model)


def resolve_feature_route(feature: str, env: Optional[Mapping[str, str]] = None) -> ResolvedFeatureRoute:
    """Resolve primary/fallback provider legs at the call boundary.

    Per-feature routes take precedence over intentional chat/translation group
    routes, then deployment-wide defaults, then the managed profile. In a
    neutral deployment the final managed profile is never an implicit fallback.
    """

    values = os.environ if env is None else env
    neutral = _neutral_deployment(values)
    spec = FEATURE_ROUTE_MANIFEST.get(feature) or _feature_route_spec(feature, _DEFAULT_CONFIG)

    provider_value = values.get(spec.provider_env, '').strip()
    model_value = values.get(spec.model_env, '').strip()
    if provider_value or model_value:
        provider = _normalize_provider(provider_value) or spec.default.provider
        model = model_value or _provider_default_model(provider, values, allow_builtin=not neutral)
        if not model and provider == spec.default.provider and not neutral:
            model = spec.default.model
        if not model:
            raise ValueError(f"Feature '{feature}' requires an explicit provider/model route in a neutral deployment")
        primary = ProviderRoute(provider=provider, model=model)
        source = 'feature_env'
    else:
        group_route = _group_route_override(feature, values, neutral_deployment=neutral)
        if group_route is not None:
            primary, source = group_route, 'group_env'
        else:
            default_provider = _normalize_provider(values.get(DEFAULT_ROUTE_PROVIDER_ENV, ''))
            default_model = values.get(DEFAULT_ROUTE_MODEL_ENV, '').strip()
            if default_provider or default_model:
                provider = default_provider or spec.default.provider
                model = default_model or _provider_default_model(provider, values, allow_builtin=not neutral)
                if not model and provider == spec.default.provider and not neutral:
                    model = spec.default.model
                if not model:
                    raise ValueError(
                        f"Feature '{feature}' requires an explicit provider/model route in a neutral deployment"
                    )
                primary, source = ProviderRoute(provider=provider, model=model), 'default_env'
            else:
                if neutral:
                    raise ValueError(
                        f"Feature '{feature}' requires an explicit provider/model route in a neutral deployment"
                    )
                primary, source = spec.default, 'profile'

    fallback_raw = values.get(spec.fallbacks_env, '').strip() or values.get(DEFAULT_ROUTE_FALLBACKS_ENV, '').strip()
    fallbacks = tuple(route for route in _parse_fallbacks(fallback_raw, feature=feature) if route != primary)
    return ResolvedFeatureRoute(feature=feature, primary=primary, fallbacks=fallbacks, source=source)


def _get_model_config(feature: str) -> Tuple[str, str]:
    """Get the (model, provider) tuple for a feature. Internal — used by get_llm/get_model/get_provider.

    Resolution order: explicit self-hosted route > pinned > active profile > fallback.
    """
    route = resolve_feature_route(feature)
    return route.primary.model, route.primary.provider


def get_model_config(feature: str) -> Tuple[str, str]:
    """Get the (model, provider) tuple for a feature.

    Resolution order: pinned > active profile > fallback.
    """
    return _get_model_config(feature)


def get_model(feature: str) -> str:
    """Get the model name for a feature from the active Model QoS profile.

    Resolution order: pinned > active profile > fallback.

    Args:
        feature: Feature name (e.g. 'conv_action_items', 'chat_agent').

    Returns:
        Model name string (e.g. 'gpt-5.6-luna', 'claude-sonnet-4-6').
    """
    return _get_model_config(feature)[0]


def get_provider(feature: str) -> str:
    """Get the provider for a feature from the active Model QoS profile.

    Returns:
        Provider string: 'openai', 'gemini', 'openrouter', 'anthropic', 'perplexity'.
    """
    return _get_model_config(feature)[1]


def get_route_options(feature: str, model: str, provider: str) -> Dict[str, object]:
    """Return provider/model construction options for a resolved route."""

    options: Dict[str, object] = {}
    if supports_cache_retention(model):
        options['extra_body'] = {"prompt_cache_retention": "24h"}
    if provider == 'openrouter':
        temperature = _OPENROUTER_TEMPERATURES.get(feature)
        if temperature is not None:
            options['temperature'] = temperature
    if provider == 'gemini' and not is_structured_output_feature(feature):
        # Structured-output features use .with_structured_output(), which routes through
        # Completions.parse() and rejects thinking_budget (issue #7898).
        options['thinking_budget'] = 0
    return options


def get_route_ref(feature: str) -> RouteRef:
    """Return the typed route reference for a feature without changing legacy routing.

    Existing features resolve to explicit provider/model refs by default. Auto-lane
    refs are opt-in through _AUTO_LANE_FEATURES and are not used by get_model(),
    get_provider(), or get_llm().
    """

    lane_id = _AUTO_LANE_FEATURES.get(feature)
    if lane_id is not None:
        if not is_auto_lane_id(lane_id):
            raise ValueError(f"Auto lane route for feature '{feature}' must use omi:auto: namespace")
        return AutoLaneRouteRef(feature=feature, lane_id=lane_id)

    model, provider = _get_model_config(feature)
    return ExplicitRouteRef(
        feature=feature,
        model=model,
        provider=provider,
        options=get_route_options(feature, model, provider),
    )


def supports_prompt_cache(model: str) -> bool:
    """Whether a model supports OpenAI prompt-cache routing (prompt_cache_key)."""
    return bool(model) and model.startswith(_CACHE_KEY_MODEL_PREFIXES)


def supports_cache_retention(model: str) -> bool:
    """Whether a model supports 24h OpenAI prompt-cache retention (prompt_cache_retention='24h')."""
    # GPT-5.6 uses the explicit cache contract (prompt_cache_options + a
    # breakpoint) rather than the legacy prompt_cache_retention field. Sending
    # both contracts in the same request is rejected by the provider.
    return bool(model) and not model.startswith('gpt-5.6') and model.startswith(_CACHE_RETENTION_MODEL_PREFIXES)


def is_structured_output_feature(feature: str) -> bool:
    return feature in _STRUCTURED_OUTPUT_FEATURES


def is_anthropic_only_feature(feature: str) -> bool:
    return feature in _ANTHROPIC_ONLY_FEATURES


def is_perplexity_only_feature(feature: str) -> bool:
    return feature in _PERPLEXITY_ONLY_FEATURES


def get_active_profile_name() -> str:
    return _active_profile_name


def get_active_profile() -> Dict[str, Tuple[str, str]]:
    return _active_profile


def get_all_configured_features() -> set[str]:
    return set(FEATURE_ROUTE_MANIFEST.keys())


def get_feature_route_manifest() -> Dict[str, FeatureRouteSpec]:
    return dict(FEATURE_ROUTE_MANIFEST)


def get_default_config() -> Tuple[str, str]:
    return _DEFAULT_CONFIG


def get_byok_profile() -> Dict[str, Tuple[str, str]]:
    return _byok_profile


def get_byok_profile_name() -> str:
    return _byok_profile_name


def get_openrouter_temperatures() -> Dict[str, float]:
    return _OPENROUTER_TEMPERATURES


def get_pinned_features() -> Dict[str, Tuple[str, str]]:
    return _PINNED_FEATURES


def get_anthropic_only_features() -> set[str]:
    return _ANTHROPIC_ONLY_FEATURES


def get_perplexity_only_features() -> set[str]:
    return _PERPLEXITY_ONLY_FEATURES
