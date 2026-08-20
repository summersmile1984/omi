"""Typed route selection for model surfaces outside ordinary chat completions."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Literal, Mapping

from utils.llm.model_config import ProviderRoute, resolve_feature_route

CapabilityStatus = Literal['selected', 'unavailable']


@dataclass(frozen=True)
class ModelCapabilityRoute:
    capability: str
    status: CapabilityStatus
    routes: tuple[ProviderRoute, ...] = ()
    transport: str = 'direct'
    reason: str | None = None
    retryable: bool = False

    @property
    def selected(self) -> bool:
        return self.status == 'selected'

    def unavailable_payload(self) -> dict[str, object]:
        return {
            'code': 'model_capability_unavailable',
            'capability': self.capability,
            'reason': self.reason or 'not_configured',
            'retryable': self.retryable,
        }

    def unavailable_tool_result(self) -> str:
        return json.dumps(self.unavailable_payload(), sort_keys=True, separators=(',', ':'))


class ModelCapabilityUnavailableError(RuntimeError):
    """Typed failure for a selected model capability that could not execute."""

    def __init__(
        self,
        capability: str,
        reason: str,
        *,
        retryable: bool = True,
    ) -> None:
        self.route = _unavailable(capability, reason, retryable=retryable)
        super().__init__(f'{capability} model capability unavailable: {reason}')

    def as_dict(self) -> dict[str, object]:
        return self.route.unavailable_payload()


_FEATURE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    'agent_chat': ('chat_agent',),
    'notes': ('conv_structure', 'conv_action_items', 'conv_app_result'),
    'memory_kg': ('memories', 'knowledge_graph'),
    'task_recommendations': ('what_matters_now', 'goals_advice'),
    'proactive': ('proactive_notification', 'desktop_proactive_reasoning'),
}


def resolve_model_capability(
    capability: str,
    *,
    requested_provider: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ModelCapabilityRoute:
    values = os.environ if env is None else env
    if capability in _FEATURE_CAPABILITIES:
        routes: list[ProviderRoute] = []
        for feature in _FEATURE_CAPABILITIES[capability]:
            resolved = resolve_feature_route(feature, values)
            for route in (resolved.primary, *resolved.fallbacks):
                if route not in routes:
                    routes.append(route)
        return ModelCapabilityRoute(capability=capability, status='selected', routes=tuple(routes))
    if capability == 'screen':
        provider = values.get('EMBEDDING_PROVIDER', 'openai').strip().lower() or 'openai'
        model = values.get('EMBEDDING_MODEL', '').strip()
        if provider == 'generic':
            model = model or values.get('GENERIC_OPENAI_MODEL', '').strip()
            if (
                not values.get('GENERIC_OPENAI_BASE_URL', '').strip()
                or not values.get('GENERIC_OPENAI_API_KEY', '').strip()
            ):
                return _unavailable(capability, 'generic_embedding_endpoint_not_configured', retryable=False)
        elif provider == 'gemini':
            model = model or 'embedding-001'
        elif provider == 'openai':
            model = model or 'text-embedding-3-large'
        if not model:
            return _unavailable(capability, 'embedding_model_not_configured', retryable=False)
        return ModelCapabilityRoute(
            capability=capability,
            status='selected',
            routes=(ProviderRoute(provider=provider, model=model),),
            transport='embedding',
        )
    if capability == 'web_search':
        transport = values.get('WEB_SEARCH_TRANSPORT', 'gateway').strip().lower() or 'gateway'
        if transport == 'disabled':
            return _unavailable(capability, 'disabled_by_deployment', retryable=False)
        if transport != 'gateway':
            return _unavailable(capability, 'unsupported_transport', retryable=False)
        route = resolve_feature_route('web_search', values)
        return ModelCapabilityRoute(
            capability=capability,
            status='selected',
            routes=(route.primary, *route.fallbacks),
            transport='gateway',
        )
    if capability == 'realtime':
        requested = (requested_provider or '').strip().lower()
        selected = values.get('REALTIME_PROVIDER', '').strip().lower()
        if selected == 'disabled':
            return _unavailable(capability, 'disabled_by_deployment', retryable=False)
        provider = selected or requested
        if provider not in {'openai', 'gemini'}:
            return _unavailable(capability, 'unsupported_provider', retryable=False)
        if selected and requested and selected != requested:
            return _unavailable(capability, 'provider_not_selected', retryable=False)
        model = values.get('REALTIME_MODEL', '').strip()
        if not model:
            model = 'gpt-realtime-2' if provider == 'openai' else 'models/gemini-3.1-flash-live-preview'
        return ModelCapabilityRoute(
            capability=capability,
            status='selected',
            routes=(ProviderRoute(provider=provider, model=model),),
            transport='ephemeral_token',
        )
    return _unavailable(capability, 'unknown_capability', retryable=False)


def _unavailable(capability: str, reason: str, *, retryable: bool = True) -> ModelCapabilityRoute:
    return ModelCapabilityRoute(
        capability=capability,
        status='unavailable',
        reason=reason,
        retryable=retryable,
    )
