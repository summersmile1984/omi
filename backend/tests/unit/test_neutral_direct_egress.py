"""Regression guards for synchronous paths outside the shared HTTP pools."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from utils import app_integrations
from utils import mcp_client
from utils.egress_policy import EgressPolicyUnavailable, assert_http_endpoint_allowed


def test_neutral_profile_rejects_github_authority_even_if_allowlisted(monkeypatch):
    """GitHub product docs are managed-only, not an operator egress target."""

    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.setenv('SELF_HOST_EGRESS_ALLOWLIST', 'api.github.com,githubusercontent.com')

    with pytest.raises(EgressPolicyUnavailable, match='official_endpoint_forbidden'):
        assert_http_endpoint_allowed('https://api.github.com/repos/BasedHardware/omi/contents/docs/doc')


def test_github_docs_guard_runs_before_cache_or_sync_transport(monkeypatch):
    """The synchronous GitHub helper must not bypass the process egress gate."""

    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)

    with patch.object(app_integrations, 'get_generic_cache', return_value={'cached': 'managed docs'}) as cache:
        with patch.object(app_integrations.httpx, 'get') as http_get:
            with pytest.raises(EgressPolicyUnavailable, match='official_endpoint_forbidden'):
                app_integrations.get_github_docs_content()

    cache.assert_not_called()
    http_get.assert_not_called()


def test_marketplace_reenable_probe_rejects_forbidden_authority_before_http(monkeypatch):
    """User-configured marketplace probes must honor the neutral egress gate."""

    from utils import apps

    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)
    with patch.object(apps.httpx, 'request') as request:
        with pytest.raises(HTTPException) as raised:
            apps.validate_app_endpoints_for_reenable(
                {'external_integration': {'webhook_url': 'https://api.openai.com/v1'}, 'chat_tools': []},
                {},
                'app-1',
            )

    assert getattr(raised.value, 'status_code', None) == 400
    request.assert_not_called()


def test_marketplace_manifest_rejects_forbidden_authority_before_cache_or_http(monkeypatch):
    """A cached or live app manifest cannot bypass neutral authority policy."""

    from utils import apps

    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)
    with (
        patch.object(apps, 'get_generic_cache', return_value={'tools': [{'name': 'managed'}]}),
        patch.object(apps.httpx, 'get') as http_get,
    ):
        result = apps.fetch_app_chat_tools_from_manifest('https://api.openai.com/.well-known/omi-tools.json')

    assert result is None
    http_get.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_transport_rejects_forbidden_authority_before_client(monkeypatch):
    """MCP discovery/tool calls cannot bypass the neutral authority policy."""

    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)
    with patch.object(mcp_client.httpx, 'AsyncClient') as client:
        with pytest.raises(EgressPolicyUnavailable, match='official_endpoint_forbidden'):
            await mcp_client._mcp_post('https://api.openai.com/mcp', {'jsonrpc': '2.0', 'id': 1})

    client.assert_not_called()
