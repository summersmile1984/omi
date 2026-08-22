from fastapi import HTTPException
import pytest

from routers.desktop_tts_updates import (
    ReleaseInfo,
    _appcast_xml,
    _is_allowed_openai_voice,
    _manual_download_url,
    _neutral_desktop_updates_disabled,
    _require_desktop_update_capability,
)


def _release(**overrides):
    values = {
        "version": "1.0.0",
        "build_number": 1,
        "download_url": "https://example.com/Omi.zip",
        "ed_signature": "signature",
        "published_at": "2026-07-26T00:00:00Z",
        "is_live": True,
    }
    values.update(overrides)
    return ReleaseInfo(**values)


def test_openai_tts_voices_match_rust_contract():
    assert _is_allowed_openai_voice("marin")
    assert _is_allowed_openai_voice("cedar")
    assert not _is_allowed_openai_voice("BAMYoBHLZM7lJgJAmFz0")


def test_appcast_deduplicates_staging_and_preserves_stable_default_channel():
    xml = _appcast_xml(
        [
            _release(version="2.0.0"),
            _release(version="1.0.0", channel="staging"),
            _release(version="3.0.0", channel="stable"),
        ],
        "macos",
    )
    assert "Omi 2.0.0" in xml
    assert "Omi 1.0.0" not in xml
    assert "Omi 3.0.0" in xml
    assert xml.count("<sparkle:channel>") == 1


def test_manual_download_prefers_explicit_dmg_then_github_zip_derivation():
    assert (
        _manual_download_url(_release(manual_download_url="https://example.com/custom.dmg"))
        == "https://example.com/custom.dmg"
    )
    assert _manual_download_url(_release()) == "https://example.com/Omi.dmg"


def test_neutral_profile_disables_legacy_desktop_update_routes(monkeypatch):
    monkeypatch.setenv("OMI_DEPLOYMENT_PROFILE", "self_hosted")
    assert _neutral_desktop_updates_disabled() is True

    monkeypatch.setenv("OMI_DEPLOYMENT_PROFILE", "omi_cloud")
    assert _neutral_desktop_updates_disabled() is False


def test_neutral_update_capability_fails_closed_before_release_firestore(monkeypatch):
    monkeypatch.setenv("OMI_DEPLOYMENT_PROFILE", "self_hosted")
    with pytest.raises(HTTPException) as raised:
        _require_desktop_update_capability()
    assert raised.value.status_code == 503
    assert raised.value.detail["reason"] == "operator_update_authority_not_configured"
