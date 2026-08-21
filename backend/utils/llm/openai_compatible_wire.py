# LIFECYCLE: permanent
"""Executable OpenAI-compatible wire targets shared by raw completion surfaces."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

import httpx

from utils.llm.direct_fallback import direct_fallback_reason
from utils.llm.model_config import ProviderRoute, resolve_feature_route
from utils.llm.providers import (
    OPENAI_COMPATIBLE_PROVIDERS,
    get_openai_compatible_provider_config,
    openai_compatible_api_model_name,
)
from utils.observability.fallback import record_fallback


@dataclass(frozen=True)
class OpenAICompatibleWireTarget:
    route: ProviderRoute
    url: str
    headers: Mapping[str, str]
    api_model: str


class OpenAICompatibleRouteConfigurationError(RuntimeError):
    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"OpenAI-compatible provider '{provider}' is not configured: {reason}")


def resolve_openai_compatible_wire_targets(
    feature: str, *, require_explicit: bool = False
) -> tuple[OpenAICompatibleWireTarget, ...]:
    """Return the configured primary plus only executable optional fallbacks.

    An unsupported primary belongs to another transport and returns no targets.
    A supported but misconfigured primary fails closed. Optional fallbacks with a
    missing endpoint or credential are filtered and can never borrow the primary's
    key.
    """

    resolved = resolve_feature_route(feature)
    if require_explicit and resolved.source == 'profile':
        return ()
    targets: list[OpenAICompatibleWireTarget] = []
    for index, route in enumerate((resolved.primary, *resolved.fallbacks)):
        if route.provider not in OPENAI_COMPATIBLE_PROVIDERS:
            if index == 0:
                if route.provider != 'anthropic' and resolved.source != 'profile':
                    raise OpenAICompatibleRouteConfigurationError(route.provider, 'wire_transport_not_supported')
                return ()
            continue
        try:
            config = get_openai_compatible_provider_config(route.provider)
            base_url = config.resolved_base_url()
        except ValueError as exc:
            if index == 0:
                raise OpenAICompatibleRouteConfigurationError(route.provider, 'endpoint_not_configured') from exc
            continue
        api_key = os.environ.get(config.api_key_env, '').strip()
        if not base_url or not api_key:
            if index == 0:
                reason = 'endpoint_not_configured' if not base_url else 'credential_not_configured'
                raise OpenAICompatibleRouteConfigurationError(route.provider, reason)
            continue
        targets.append(
            OpenAICompatibleWireTarget(
                route=route,
                url=f'{base_url.rstrip("/")}/chat/completions',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                    **config.default_headers,
                },
                api_model=openai_compatible_api_model_name(config, route.model),
            )
        )
    return tuple(targets)


async def post_with_bounded_openai_fallback(
    targets: tuple[OpenAICompatibleWireTarget, ...],
    payload_for: Callable[[OpenAICompatibleWireTarget], Mapping[str, object]],
    post: Callable[[OpenAICompatibleWireTarget, Mapping[str, object]], Awaitable[httpx.Response]],
) -> tuple[httpx.Response, OpenAICompatibleWireTarget]:
    """Execute the shared retryable-only direct fallback contract."""

    if not targets:
        raise ValueError('at least one executable OpenAI-compatible target is required')
    primary = targets[0]
    first_reason: str | None = None
    for index, target in enumerate(targets):
        try:
            response = await post(target, payload_for(target))
            response.raise_for_status()
            if index:
                record_openai_compatible_fallback(primary, target, first_reason or 'other', 'recovered')
            return response, target
        except Exception as exc:
            reason = direct_fallback_reason(exc)
            if reason is None or index == len(targets) - 1:
                if first_reason is not None:
                    record_openai_compatible_fallback(primary, None, first_reason, 'exhausted')
                raise
            first_reason = first_reason or reason
    raise RuntimeError('bounded OpenAI-compatible route exhausted')


def record_openai_compatible_fallback(
    primary: OpenAICompatibleWireTarget,
    target: OpenAICompatibleWireTarget | None,
    reason: str,
    outcome: str,
) -> None:
    record_fallback(
        component='llm_gateway',
        from_mode=f'{primary.route.provider}:{primary.route.model}',
        to_mode=f'{target.route.provider}:{target.route.model}' if target is not None else 'none',
        reason=reason,
        outcome=outcome,
    )
