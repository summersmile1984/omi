"""Runtime tests for the self-host HTTP authority boundary."""

import httpx
import pytest

from utils.egress_policy import EgressPolicyUnavailable, assert_http_endpoint_allowed, enforce_httpx_request
from utils.http_client import close_all_clients, get_web_fetch_client


def test_managed_profile_preserves_existing_endpoint_behavior(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'omi_cloud')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)

    assert assert_http_endpoint_allowed('https://api.openai.com/v1') == 'api.openai.com'


@pytest.mark.parametrize('host', ['api.openai.com', 'api.omi.me', 'generativelanguage.googleapis.com', 'api.hume.ai'])
def test_neutral_profile_rejects_official_hosts_before_allowlist(monkeypatch, host):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.setenv('SELF_HOST_EGRESS_ALLOWLIST', 'operator.example')

    with pytest.raises(EgressPolicyUnavailable, match='official_endpoint_forbidden'):
        assert_http_endpoint_allowed(f'https://{host}/v1')


def test_neutral_profile_requires_allowlist_for_external_host(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'neutral')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)

    with pytest.raises(EgressPolicyUnavailable, match='egress_allowlist_not_configured'):
        assert_http_endpoint_allowed('https://operator.example/v1')


def test_neutral_profile_allows_internal_compose_authority_without_external_permission(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)

    assert assert_http_endpoint_allowed('http://auth-server:3000/api/auth/jwks') == 'auth-server'


def test_neutral_profile_allows_exact_and_wildcard_operator_hosts(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.setenv('SELF_HOST_EGRESS_ALLOWLIST', 'llm.example,realtime.example')

    assert assert_http_endpoint_allowed('https://llm.example/v1') == 'llm.example'
    assert assert_http_endpoint_allowed('https://realtime.example/v1') == 'realtime.example'

    monkeypatch.setenv('SELF_HOST_EGRESS_ALLOWLIST', '*.operator.example')
    assert assert_http_endpoint_allowed('https://llm.operator.example/v1') == 'llm.operator.example'


def test_neutral_profile_rejects_invalid_or_official_allowlist(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.setenv('SELF_HOST_EGRESS_ALLOWLIST', 'https://operator.example')
    with pytest.raises(EgressPolicyUnavailable, match='invalid_egress_allowlist'):
        assert_http_endpoint_allowed('https://operator.example/v1')

    monkeypatch.setenv('SELF_HOST_EGRESS_ALLOWLIST', 'api.openai.com')
    with pytest.raises(EgressPolicyUnavailable, match='official_host_in_egress_allowlist'):
        assert_http_endpoint_allowed('https://operator.example/v1')


@pytest.mark.asyncio
async def test_httpx_hook_rejects_before_transport(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.setenv('SELF_HOST_EGRESS_ALLOWLIST', 'operator.example')
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        event_hooks={'request': [enforce_httpx_request]},
    ) as client:
        with pytest.raises(EgressPolicyUnavailable, match='endpoint_not_allowlisted') as error:
            await client.get('https://undeclared.example/v1')
        assert error.value.code == 'deployment_capability_unavailable'
        assert error.value.retryable is False
        response = await client.get('https://operator.example/v1')

    assert response.status_code == 200
    assert calls == ['https://operator.example/v1']


@pytest.mark.asyncio
async def test_shared_client_pool_carries_runtime_hook(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)

    client = get_web_fetch_client()
    try:
        with pytest.raises(EgressPolicyUnavailable, match='egress_allowlist_not_configured'):
            await client.get('https://undeclared.example/v1')
    finally:
        await close_all_clients()
