"""Typed route selection for model surfaces outside ordinary chat completions."""

from __future__ import annotations

import json
import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Literal, Mapping
from urllib.parse import urlparse

from utils.llm.model_config import ProviderRoute, resolve_feature_route
from utils.llm.providers import OPENAI_COMPATIBLE_PROVIDERS, get_openai_compatible_provider_config

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

_RELAY_PROVIDER_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
REALTIME_RELAY_WIRE_PROTOCOLS = frozenset({'openai_realtime_v1'})


def _neutral_deployment(values: Mapping[str, str]) -> bool:
    return values.get('OMI_DEPLOYMENT_PROFILE', '').strip().lower() in {
        'neutral',
        'self_hosted',
        'self-hosted',
    }


def _capability_transport(values: Mapping[str, str], name: str, managed_default: str) -> str:
    configured = values.get(name, '').strip().lower()
    if configured:
        return configured
    return 'disabled' if _neutral_deployment(values) else managed_default


def realtime_relay_wire_protocol(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    return values.get('REALTIME_RELAY_WIRE_PROTOCOL', '').strip().lower()


def _private_relay_host(host: str) -> bool:
    if host == 'localhost' or '.' not in host or host.endswith(('.internal', '.svc', '.svc.cluster.local')):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    networks = (
        ipaddress.ip_network('10.0.0.0/8'),
        ipaddress.ip_network('172.16.0.0/12'),
        ipaddress.ip_network('192.168.0.0/16'),
        ipaddress.ip_network('fc00::/7'),
    )
    return any(address in network for network in networks)


def _unsafe_relay_host(host: str) -> bool:
    if host in {'metadata', 'metadata.google.internal'}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_link_local or address.is_unspecified or address.is_multicast or address.is_reserved


def _embedding_capability(capability: str, values: Mapping[str, str]) -> ModelCapabilityRoute:
    provider = values.get('EMBEDDING_PROVIDER', '').strip().lower()
    if not provider:
        if _neutral_deployment(values):
            return _unavailable(capability, 'embedding_provider_not_configured', retryable=False)
        provider = 'openai'
    model = values.get('EMBEDDING_MODEL', '').strip()
    if provider == 'generic':
        model = model or values.get('GENERIC_OPENAI_MODEL', '').strip()
        if (
            not values.get('GENERIC_OPENAI_BASE_URL', '').strip()
            or not values.get('GENERIC_OPENAI_API_KEY', '').strip()
        ):
            return _unavailable(capability, 'generic_embedding_endpoint_not_configured', retryable=False)
    elif provider == 'gemini':
        if _neutral_deployment(values):
            return _unavailable(capability, 'official_provider_forbidden', retryable=False)
        if not values.get('GEMINI_API_KEY', '').strip() and not (
            values.get('USE_VERTEX_AI', '').strip().lower() == 'true' and values.get('GOOGLE_CLOUD_PROJECT', '').strip()
        ):
            return _unavailable(capability, 'gemini_embedding_credential_not_configured', retryable=False)
        model = model or 'embedding-001'
    elif provider == 'openai':
        if _neutral_deployment(values):
            return _unavailable(capability, 'official_provider_forbidden', retryable=False)
        if not values.get('OPENAI_API_KEY', '').strip():
            return _unavailable(capability, 'openai_embedding_credential_not_configured', retryable=False)
        model = model or 'text-embedding-3-large'
    elif provider in OPENAI_COMPATIBLE_PROVIDERS:
        if _neutral_deployment(values) and provider != 'generic':
            return _unavailable(capability, 'official_provider_forbidden', retryable=False)
        config = get_openai_compatible_provider_config(provider)
        try:
            config.resolved_base_url(values)
        except ValueError:
            return _unavailable(capability, f'{provider}_embedding_endpoint_not_configured', retryable=False)
        if not values.get(config.api_key_env, '').strip():
            return _unavailable(capability, f'{provider}_embedding_credential_not_configured', retryable=False)
        model = model or values.get(f'{provider.upper()}_MODEL', '').strip()
    if not model:
        return _unavailable(capability, 'embedding_model_not_configured', retryable=False)
    return ModelCapabilityRoute(
        capability=capability,
        status='selected',
        routes=(ProviderRoute(provider=provider, model=model),),
        transport='embedding',
    )


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
            try:
                resolved = resolve_feature_route(feature, values)
            except ValueError:
                return _unavailable(capability, 'provider_route_not_configured', retryable=False)
            for route in (resolved.primary, *resolved.fallbacks):
                if route not in routes:
                    routes.append(route)
        return ModelCapabilityRoute(capability=capability, status='selected', routes=tuple(routes))
    if capability == 'screen':
        return _embedding_capability(capability, values)
    if capability == 'embedding':
        transport = values.get('EMBEDDING_CAPABILITY_TRANSPORT', 'disabled').strip().lower() or 'disabled'
        if transport == 'disabled':
            return _unavailable(capability, 'disabled_by_deployment', retryable=False)
        if transport != 'direct':
            return _unavailable(capability, 'unsupported_transport', retryable=False)
        return _embedding_capability(capability, values)
    if capability == 'proactive_tools':
        transport = values.get('PROACTIVE_TOOL_TRANSPORT', 'disabled').strip().lower() or 'disabled'
        if transport == 'disabled':
            return _unavailable(capability, 'disabled_by_deployment', retryable=False)
        if transport != 'completion':
            return _unavailable(capability, 'unsupported_transport', retryable=False)
        try:
            route = resolve_feature_route('desktop_proactive_reasoning', values)
        except ValueError:
            return _unavailable(capability, 'provider_route_not_configured', retryable=False)
        return ModelCapabilityRoute(
            capability=capability,
            status='selected',
            routes=(route.primary, *route.fallbacks),
            transport='tool_completion',
        )
    if capability == 'app_icon_generation':
        transport = _capability_transport(values, 'APP_ICON_GENERATION_TRANSPORT', 'gateway')
        if transport == 'disabled':
            return _unavailable(capability, 'disabled_by_deployment', retryable=False)
        if transport == 'local_template':
            return ModelCapabilityRoute(
                capability=capability,
                status='selected',
                routes=(),
                transport='local_template_image_generation',
            )
        if transport == 'openai_compatible':
            base_url = values.get('IMAGE_GENERATION_OPENAI_COMPATIBLE_BASE_URL', '').strip()
            api_key = values.get('IMAGE_GENERATION_OPENAI_COMPATIBLE_API_KEY', '').strip()
            model = values.get('IMAGE_GENERATION_OPENAI_COMPATIBLE_MODEL', '').strip()
            parsed = urlparse(base_url)
            if (
                parsed.scheme not in {'http', 'https'}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                return _unavailable(capability, 'compatible_endpoint_not_configured', retryable=False)
            if not api_key:
                return _unavailable(capability, 'compatible_credential_not_configured', retryable=False)
            if not model:
                return _unavailable(capability, 'compatible_model_not_configured', retryable=False)
            return ModelCapabilityRoute(
                capability=capability,
                status='selected',
                routes=(ProviderRoute(provider='openai_compatible', model=model),),
                transport='openai_compatible_image_generation',
            )
        if transport != 'gateway':
            return _unavailable(capability, 'unsupported_transport', retryable=False)
        return ModelCapabilityRoute(
            capability=capability,
            status='selected',
            routes=(ProviderRoute(provider='openai', model='dall-e-3'),),
            transport='gateway_image_generation',
        )
    if capability == 'file_chat':
        transport = _capability_transport(values, 'FILE_CHAT_TRANSPORT', 'openai_assistants')
        if transport == 'disabled':
            return _unavailable(capability, 'disabled_by_deployment', retryable=False)
        if transport == 'local_extraction':
            if not values.get('BUCKET_CHAT_FILES', '').strip():
                return _unavailable(capability, 'object_storage_not_configured', retryable=False)
            try:
                route = resolve_feature_route('chat_responses', values)
            except ValueError:
                return _unavailable(capability, 'provider_route_not_configured', retryable=False)
            return ModelCapabilityRoute(
                capability=capability,
                status='selected',
                routes=(route.primary, *route.fallbacks),
                transport='local_extraction',
            )
        if transport != 'openai_assistants':
            return _unavailable(capability, 'unsupported_transport', retryable=False)
        if _neutral_deployment(values):
            return _unavailable(capability, 'official_provider_forbidden', retryable=False)
        if not values.get('OPENAI_API_KEY', '').strip():
            return _unavailable(capability, 'openai_credential_not_configured', retryable=False)
        return ModelCapabilityRoute(
            capability=capability,
            status='selected',
            routes=(ProviderRoute(provider='openai', model='gpt-4.1'),),
            transport='openai_files_assistants',
        )
    if capability == 'desktop_vendor_proxy':
        transport = _capability_transport(values, 'DESKTOP_VENDOR_PROXY_TRANSPORT', 'gemini')
        if transport == 'disabled':
            return _unavailable(capability, 'disabled_by_deployment', retryable=False)
        if transport != 'gemini':
            return _unavailable(capability, 'unsupported_transport', retryable=False)
        if _neutral_deployment(values):
            return _unavailable(capability, 'official_provider_forbidden', retryable=False)
        return ModelCapabilityRoute(
            capability=capability,
            status='selected',
            routes=(ProviderRoute(provider='gemini', model='deployment_selected'),),
            transport='gemini_proxy',
        )
    if capability == 'web_search':
        transport = _capability_transport(values, 'WEB_SEARCH_TRANSPORT', 'gateway')
        if transport == 'disabled':
            return _unavailable(capability, 'disabled_by_deployment', retryable=False)
        if transport == 'searxng':
            if not values.get('SEARXNG_BASE_URL', '').strip():
                return _unavailable(capability, 'searxng_endpoint_not_configured', retryable=False)
            return ModelCapabilityRoute(capability=capability, status='selected', transport='searxng')
        if transport != 'gateway':
            return _unavailable(capability, 'unsupported_transport', retryable=False)
        try:
            route = resolve_feature_route('web_search', values)
        except ValueError:
            return _unavailable(capability, 'provider_route_not_configured', retryable=False)
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
        # A neutral process must not let a client-selected provider become an
        # implicit managed endpoint. Operators may still opt into any
        # supported provider (or the self-hosted relay), but the server-side
        # deployment contract must name it explicitly first.
        if _neutral_deployment(values) and not selected:
            return _unavailable(capability, 'disabled_by_deployment', retryable=False)
        provider = selected or requested
        if provider == 'relay':
            url = values.get('REALTIME_RELAY_URL', '').strip()
            api_key = values.get('REALTIME_RELAY_API_KEY', '').strip()
            provider_id = values.get('REALTIME_RELAY_PROVIDER_ID', '').strip().lower()
            wire_protocol = realtime_relay_wire_protocol(values)
            model = values.get('REALTIME_MODEL', '').strip()
            allowed_hosts = {
                item.strip().lower()
                for item in values.get('REALTIME_RELAY_ALLOWED_HOSTS', '').split(',')
                if item.strip()
            }
            parsed = urlparse(url)
            if (
                parsed.scheme not in {'ws', 'wss'}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                return _unavailable(capability, 'relay_url_not_configured', retryable=False)
            if not allowed_hosts or parsed.hostname.lower() not in allowed_hosts:
                return _unavailable(capability, 'relay_host_not_allowed', retryable=False)
            if _unsafe_relay_host(parsed.hostname.lower()):
                return _unavailable(capability, 'relay_host_not_allowed', retryable=False)
            if parsed.scheme == 'ws' and not _private_relay_host(parsed.hostname.lower()):
                return _unavailable(capability, 'relay_url_requires_tls', retryable=False)
            if not api_key:
                return _unavailable(capability, 'relay_credential_not_configured', retryable=False)
            if not provider_id or not _RELAY_PROVIDER_ID.fullmatch(provider_id):
                return _unavailable(capability, 'relay_provider_id_not_configured', retryable=False)
            if wire_protocol not in REALTIME_RELAY_WIRE_PROTOCOLS:
                return _unavailable(capability, 'relay_wire_protocol_not_supported', retryable=False)
            if not model:
                return _unavailable(capability, 'realtime_model_not_configured', retryable=False)
            for name, default, minimum, maximum in (
                ('REALTIME_RELAY_MAX_MESSAGE_BYTES', '1048576', 1024, 8_388_608),
                ('REALTIME_RELAY_MAX_SESSION_SECONDS', '1800', 1, 3600),
            ):
                try:
                    value = int(values.get(name, default).strip())
                except ValueError:
                    return _unavailable(capability, 'invalid_relay_limits', retryable=False)
                if value < minimum or value > maximum:
                    return _unavailable(capability, 'invalid_relay_limits', retryable=False)
            return ModelCapabilityRoute(
                capability=capability,
                status='selected',
                routes=(ProviderRoute(provider=provider_id, model=model),),
                transport='websocket_relay',
            )
        if provider not in {'openai', 'gemini'}:
            return _unavailable(capability, 'unsupported_provider', retryable=False)
        # The direct realtime handlers below construct the official OpenAI or
        # Gemini token endpoint from the provider id.  An explicit provider
        # value must not turn that hard-coded URL into a neutral deployment's
        # implicit vendor route; self-hosted realtime uses the operator-owned
        # relay contract instead.
        if _neutral_deployment(values):
            return _unavailable(capability, 'official_provider_forbidden', retryable=False)
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
