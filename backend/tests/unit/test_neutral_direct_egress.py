"""Regression guards for synchronous paths outside the shared HTTP pools."""

from unittest.mock import patch

import pytest

from utils import app_integrations
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
