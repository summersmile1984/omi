import base64
import asyncio
import hashlib
import hmac
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import entry  # noqa: E402

from entry import DEVICE_PREFIXES, _asset_key, _firmware_metadata, _firmware_response  # noqa: E402


class FakeDb:
    def __init__(self):
        self.row = None
        self.onboarding_row = None
        self.privacy_row = None

    def prepare(self, sql):
        return FakeStatement(self, sql)


class FakeStatement:
    def __init__(self, db, sql):
        self.db = db
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def first(self):
        if self.sql.startswith("SELECT single_language_mode"):
            return self.db.row
        if self.sql.startswith("SELECT completed"):
            return self.db.onboarding_row
        if self.sql.startswith("SELECT store_recording_permission"):
            return self.db.privacy_row
        raise AssertionError(f"unexpected query: {self.sql}")

    async def run(self):
        if self.sql.startswith("INSERT INTO cf_user_transcription_preferences"):
            (
                uid,
                single_language_mode,
                vocabulary_json,
                language,
                uses_custom_stt,
                custom_stt_since,
                created_at,
                updated_at,
            ) = self.args
            self.db.row = {
                "uid": uid,
                "single_language_mode": single_language_mode,
                "vocabulary_json": vocabulary_json,
                "language": language,
                "uses_custom_stt": uses_custom_stt,
                "custom_stt_since": custom_stt_since,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT INTO cf_user_onboarding"):
            uid, completed, acquisition_source, device_onboarding_completed, created_at, updated_at = self.args
            self.db.onboarding_row = {
                "uid": uid,
                "completed": completed,
                "acquisition_source": acquisition_source,
                "device_onboarding_completed": device_onboarding_completed,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT INTO cf_user_privacy_settings"):
            uid, store_recording_permission, private_cloud_sync_enabled, created_at, updated_at = self.args
            self.db.privacy_row = {
                "uid": uid,
                "store_recording_permission": store_recording_permission,
                "private_cloud_sync_enabled": private_cloud_sync_enabled,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        raise AssertionError(f"unexpected query: {self.sql}")


class FakeRequest:
    def __init__(self, env, headers, body=None, query=None):
        self.scope = {"env": env}
        self.headers = headers
        self.body = body
        self.query_params = query or {}

    async def json(self):
        return self.body


def signed_context(secret: str, uid: str = "user-1") -> tuple[str, str]:
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return encoded, base64.urlsafe_b64encode(signature).decode().rstrip("=")


def test_asset_keys_are_uid_scoped_and_reject_traversal():
    assert _asset_key("user-1", "audio/clip.wav") == "user-1/audio/clip.wav"
    assert _asset_key("user-1", "../other-user/clip.wav") is None
    assert _asset_key("user-1", "") is None


def test_firmware_metadata_preserves_release_contract():
    body = """<!-- KEY_VALUE_START
release_firmware_version: 3.0.21
ota_update_steps: erase, flash
changelog: Fix audio | Improve battery
KEY_VALUE_END -->"""
    release = {
        "body": body,
        "assets": [{"name": "Omi_CV1_OTA_v3.0.21.zip", "browser_download_url": "https://example.test/fw.zip"}],
    }
    assert DEVICE_PREFIXES["Omi CV 1"] == "Omi_CV1"
    assert _firmware_metadata(body)["ota_update_steps"] == ["erase", "flash"]
    assert _firmware_response("Omi_CV1", release)["zip_url"] == "https://example.test/fw.zip"


def test_firmware_route_uses_worker_fetch(monkeypatch):
    release = {
        "tag_name": "Omi_CV1_v3.0.21",
        "published_at": "2026-08-27T00:00:00Z",
        "body": "<!-- KEY_VALUE_START\nrelease_firmware_version: 3.0.21\nKEY_VALUE_END -->",
        "assets": [{"name": "Omi_CV1_OTA_v3.0.21.zip", "browser_download_url": "https://example.test/fw.zip"}],
    }

    class FakeResponse:
        status = 200

        async def json(self):
            return [release]

    calls = {}

    async def fake_fetch(url, **options):
        calls["url"] = url
        calls["options"] = options
        return FakeResponse()

    monkeypatch.setattr(entry, "worker_fetch", fake_fetch)
    request = type("Request", (), {"scope": {"env": type("Env", (), {})()}})()

    result = asyncio.run(entry.firmware_stable("Omi CV 1", request))

    assert calls["url"].endswith("/releases?per_page=100")
    assert calls["options"]["headers"]["user-agent"] == "omi-cloudflare-worker/0.1"
    assert result["version"] == "3.0.21"


def test_firmware_latest_and_version_routes_share_release_adapter(monkeypatch):
    release = {
        "tag_name": "Omi_CV1_v3.0.21",
        "published_at": "2026-08-27T00:00:00Z",
        "body": (
            "<!-- KEY_VALUE_START\nrelease_firmware_version: 3.0.21\n"
            "minimum_firmware_required: 3.0.6\nKEY_VALUE_END -->"
        ),
        "assets": [{"name": "Omi_CV1_OTA_v3.0.21.zip", "browser_download_url": "https://example.test/fw.zip"}],
    }

    class FakeResponse:
        status = 200

        async def json(self):
            return [release]

    async def fake_fetch(url, **options):
        return FakeResponse()

    monkeypatch.setattr(entry, "worker_fetch", fake_fetch)
    request = type("Request", (), {"scope": {"env": type("Env", (), {})()}})()

    latest = asyncio.run(entry.firmware_latest("Omi CV 1", "3.0.6", "", "", request))
    exact = asyncio.run(entry.firmware_version("Omi CV 1", "v3.0.21", request))

    assert latest["version"] == "3.0.21"
    assert exact["zip_url"] == "https://example.test/fw.zip"


def test_transcription_preferences_are_uid_scoped_and_round_trip_through_d1():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET=secret)

    initial = asyncio.run(entry.get_transcription_preferences(FakeRequest(env, headers)))
    assert initial == {
        "single_language_mode": False,
        "vocabulary": [],
        "language": "",
        "uses_custom_stt": False,
        "custom_stt_since": None,
    }

    updated = asyncio.run(
        entry.update_transcription_preferences(
            FakeRequest(
                env,
                headers,
                {"single_language_mode": True, "vocabulary": [f"term-{i}" for i in range(101)]},
            )
        )
    )
    assert updated == {"status": "ok"}

    stored = asyncio.run(entry.get_transcription_preferences(FakeRequest(env, headers)))
    assert stored["single_language_mode"] is True
    assert len(stored["vocabulary"]) == 100
    assert stored["vocabulary"][-1] == "term-99"


def test_language_routes_normalize_alias_and_update_single_language_mode():
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="language-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET=secret)

    response = asyncio.run(entry.set_user_language(FakeRequest(env, headers, {"language": "japanese"})))
    assert response == {"status": "ok", "single_language_mode": False}

    language = asyncio.run(entry.get_user_language(FakeRequest(env, headers)))
    assert language == {"language": "ja"}

    available = asyncio.run(entry.available_languages(FakeRequest(env, headers)))
    assert available["languages"][0] == {"code": "en", "name": "English"}
    assert {item["code"] for item in available["languages"]} >= {"en-US", "zh-Hans", "ja"}


def test_language_route_rejects_unsupported_values():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET=secret)

    response = asyncio.run(entry.set_user_language(FakeRequest(env, headers, {"language": "not-a-language"})))
    assert response.status_code == 400


def test_onboarding_state_is_partial_and_uid_scoped():
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="onboarding-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET=secret)

    initial = asyncio.run(entry.get_onboarding_state(FakeRequest(env, headers)))
    assert initial == {"completed": False, "acquisition_source": "", "device_onboarding_completed": False}

    updated = asyncio.run(
        entry.update_onboarding_state(
            FakeRequest(
                env,
                headers,
                {"completed": True, "acquisition_source": "desktop", "device_onboarding_completed": True},
            )
        )
    )
    assert updated == {"status": "ok"}

    partial = asyncio.run(entry.update_onboarding_state(FakeRequest(env, headers, {"completed": False})))
    assert partial == {"status": "ok"}
    assert asyncio.run(entry.get_onboarding_state(FakeRequest(env, headers))) == {
        "completed": False,
        "acquisition_source": "desktop",
        "device_onboarding_completed": True,
    }


def test_privacy_settings_preserve_defaults_and_accept_query_boolean_contract():
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="privacy-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET=secret)

    assert asyncio.run(entry.get_store_recording_permission(FakeRequest(env, headers))) == {
        "store_recording_permission": False
    }
    assert asyncio.run(entry.get_private_cloud_sync(FakeRequest(env, headers))) == {"private_cloud_sync_enabled": True}

    assert asyncio.run(
        entry.set_store_recording_permission(FakeRequest(env, headers, query={"value": "true"}))
    ) == {"status": "ok"}
    assert asyncio.run(entry.set_private_cloud_sync(FakeRequest(env, headers, query={"value": "false"}))) == {
        "status": "ok"
    }
    assert asyncio.run(entry.get_store_recording_permission(FakeRequest(env, headers))) == {
        "store_recording_permission": True
    }
    assert asyncio.run(entry.get_private_cloud_sync(FakeRequest(env, headers))) == {"private_cloud_sync_enabled": False}

    invalid = asyncio.run(entry.set_private_cloud_sync(FakeRequest(env, headers, query={"value": "maybe"})))
    assert invalid.status_code == 400
