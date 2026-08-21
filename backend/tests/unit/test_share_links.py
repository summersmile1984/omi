"""Unit coverage for OMI_SHARE_BASE_URL (#4339)."""

import pytest

from utils import share_links
from utils.conversations.search import parse_exact_conversation_reference

CONVERSATION_ID = "e8c05000-52f0-4a95-951c-ccd715523429"


def test_share_base_url_defaults_to_production(monkeypatch):
    monkeypatch.delenv("OMI_SHARE_BASE_URL", raising=False)
    monkeypatch.delenv("OMI_DEPLOYMENT_PROFILE", raising=False)
    assert share_links.share_base_url() == "https://h.omi.me"
    assert share_links.build_share_url("/chat/abc") == "https://h.omi.me/chat/abc"
    assert share_links.share_host() == "h.omi.me"


def test_share_base_url_honors_env_override(monkeypatch):
    monkeypatch.setenv("OMI_SHARE_BASE_URL", "https://share.example.com/")
    assert share_links.share_base_url() == "https://share.example.com"
    assert share_links.build_share_url("/tasks/tok") == "https://share.example.com/tasks/tok"
    assert share_links.share_host() == "share.example.com"
    assert "h.omi.me" in share_links.accepted_share_hosts()
    assert "share.example.com" in share_links.accepted_share_hosts()


def test_parse_exact_conversation_reference_accepts_configured_host(monkeypatch):
    monkeypatch.setenv("OMI_SHARE_BASE_URL", "https://share.example.com:8443/omi/")
    assert (
        parse_exact_conversation_reference(f"https://share.example.com:8443/omi/conversations/{CONVERSATION_ID}")
        == CONVERSATION_ID
    )
    # Production links still resolve for self-hosted deployments.
    monkeypatch.setenv("OMI_SHARE_BASE_URL", "https://share.example.com")
    assert parse_exact_conversation_reference(f"https://h.omi.me/conversations/{CONVERSATION_ID}") == CONVERSATION_ID


def test_parse_exact_conversation_reference_rejects_malformed_port(monkeypatch):
    monkeypatch.delenv("OMI_SHARE_BASE_URL", raising=False)
    assert parse_exact_conversation_reference(f"https://h.omi.me:bad/conversations/{CONVERSATION_ID}") is None
    assert parse_exact_conversation_reference(f"https://h.omi.me:99999/conversations/{CONVERSATION_ID}") is None


def test_share_base_url_adds_https_when_scheme_missing(monkeypatch):
    monkeypatch.delenv("OMI_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.setenv("OMI_SHARE_BASE_URL", "share.example.com")
    assert share_links.share_base_url() == "https://share.example.com"


def test_self_hosted_share_origin_is_required_and_does_not_accept_omi(monkeypatch):
    monkeypatch.setenv("OMI_DEPLOYMENT_PROFILE", "self_hosted")
    monkeypatch.delenv("OMI_SHARE_BASE_URL", raising=False)

    with pytest.raises(share_links.ShareOriginUnavailableError):
        share_links.share_base_url()
    assert share_links.accepted_share_hosts() == frozenset()
    assert parse_exact_conversation_reference(f"https://h.omi.me/conversations/{CONVERSATION_ID}") is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://share.example.com",
        "https://h.omi.me",
        "https://share.omi.me",
        "https://share.example.com/path",
        "https://user:pass@share.example.com",
    ],
)
def test_self_hosted_share_origin_rejects_noncanonical_or_omi_values(monkeypatch, origin):
    monkeypatch.setenv("OMI_DEPLOYMENT_PROFILE", "self-hosted")
    monkeypatch.setenv("OMI_SHARE_BASE_URL", origin)
    with pytest.raises(share_links.ShareOriginUnavailableError):
        share_links.share_base_url()


def test_self_hosted_share_origin_is_canonical_and_only_operator_host_is_accepted(monkeypatch):
    monkeypatch.setenv("OMI_DEPLOYMENT_PROFILE", "neutral")
    monkeypatch.setenv("OMI_SHARE_BASE_URL", "https://Share.Example.COM:443/")
    assert share_links.share_base_url() == "https://share.example.com"
    assert share_links.accepted_share_hosts() == frozenset({"share.example.com"})
    assert (
        parse_exact_conversation_reference(f"https://share.example.com/conversations/{CONVERSATION_ID}")
        == CONVERSATION_ID
    )
    assert parse_exact_conversation_reference(f"https://h.omi.me/conversations/{CONVERSATION_ID}") is None
