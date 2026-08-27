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

from entry import (  # noqa: E402
    DEVICE_PREFIXES,
    _asset_key,
    _etag_matches,
    _firmware_metadata,
    _firmware_response,
    _parse_asset_range,
)


class FakeDb:
    def __init__(self):
        self.row = None
        self.onboarding_row = None
        self.privacy_row = None
        self.training_data_opt_in_row = None
        self.fcm_token_row = None
        self.webhook_rows = {}
        self.notification_row = None
        self.notification_preferences_row = None
        self.geolocation_row = None
        self.location_row = None
        self.assistant_settings_row = None
        self.ai_profile_row = None
        self.asset_row = None

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
        if self.sql.startswith("SELECT status FROM cf_user_training_data_opt_in"):
            return self.db.training_data_opt_in_row
        if self.sql.startswith("SELECT url, enabled FROM cf_user_developer_webhooks"):
            uid, webhook_type = self.args
            return self.db.webhook_rows.get((uid, webhook_type))
        if self.sql.startswith("SELECT notifications_enabled"):
            return self.db.notification_row
        if self.sql.startswith("SELECT daily_summary_enabled"):
            return self.db.notification_preferences_row
        if self.sql.startswith("SELECT google_place_id, latitude, longitude"):
            return self.db.geolocation_row
        if self.sql.startswith("SELECT status, purpose, disclosed_providers_json"):
            return self.db.location_row
        if self.sql.startswith("SELECT settings_json"):
            return self.db.assistant_settings_row
        if self.sql.startswith("SELECT profile_text, generated_at, data_sources_used"):
            return self.db.ai_profile_row
        if self.sql.startswith("SELECT content_type, etag, size, checksum_sha256"):
            return self.db.asset_row
        raise AssertionError(f"unexpected query: {self.sql}")

    async def run(self):
        if self.sql.startswith("INSERT INTO cf_asset_objects"):
            uid, object_key, content_type, size, etag, checksum_sha256, created_at, updated_at = self.args
            self.db.asset_row = {
                "uid": uid,
                "object_key": object_key,
                "content_type": content_type,
                "size": size,
                "etag": etag,
                "checksum_sha256": checksum_sha256,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("DELETE FROM cf_asset_objects"):
            self.db.asset_row = None
            return
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
        if self.sql.startswith("INSERT INTO cf_user_training_data_opt_in"):
            uid, status, requested_at, created_at, updated_at = self.args
            self.db.training_data_opt_in_row = {
                "uid": uid,
                "status": status,
                "requested_at": requested_at,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT INTO cf_user_fcm_tokens"):
            uid, device_key, token, time_zone, created_at, updated_at = self.args
            self.db.fcm_token_row = {
                "uid": uid,
                "device_key": device_key,
                "token": token,
                "time_zone": time_zone,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT INTO cf_user_developer_webhooks"):
            uid, webhook_type, url, enabled, created_at, updated_at = self.args
            self.db.webhook_rows[(uid, webhook_type)] = {
                "uid": uid,
                "webhook_type": webhook_type,
                "url": url,
                "enabled": enabled,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT INTO cf_user_notification_settings"):
            uid, notifications_enabled, notification_frequency, created_at, updated_at = self.args
            self.db.notification_row = {
                "uid": uid,
                "notifications_enabled": notifications_enabled,
                "notification_frequency": notification_frequency,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT INTO cf_user_notification_preferences"):
            (
                uid,
                daily_summary_enabled,
                daily_summary_hour_local,
                mentor_notification_frequency,
                created_at,
                updated_at,
            ) = self.args
            self.db.notification_preferences_row = {
                "uid": uid,
                "daily_summary_enabled": daily_summary_enabled,
                "daily_summary_hour_local": daily_summary_hour_local,
                "mentor_notification_frequency": mentor_notification_frequency,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT INTO cf_user_geolocation"):
            uid, google_place_id, latitude, longitude, address, location_type, updated_at, expires_at = self.args
            self.db.geolocation_row = {
                "uid": uid,
                "google_place_id": google_place_id,
                "latitude": latitude,
                "longitude": longitude,
                "address": address,
                "location_type": location_type,
                "updated_at": updated_at,
                "expires_at": expires_at,
            }
            return
        if self.sql.startswith("DELETE FROM cf_user_geolocation"):
            self.db.geolocation_row = None
            return
        if self.sql.startswith("INSERT INTO cf_user_location_context_consent"):
            (
                uid,
                status,
                purpose,
                disclosed_providers_json,
                granted_at,
                expires_at,
                revoked_at,
                created_at,
                updated_at,
            ) = self.args
            self.db.location_row = {
                "uid": uid,
                "status": status,
                "purpose": purpose,
                "disclosed_providers_json": disclosed_providers_json,
                "granted_at": granted_at,
                "expires_at": expires_at,
                "revoked_at": revoked_at,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT INTO cf_user_assistant_settings"):
            uid, settings_json, created_at, updated_at = self.args
            self.db.assistant_settings_row = {
                "uid": uid,
                "settings_json": settings_json,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT INTO cf_user_ai_profiles"):
            uid, profile_text, generated_at, data_sources_used, created_at, updated_at = self.args
            self.db.ai_profile_row = {
                "uid": uid,
                "profile_text": profile_text,
                "generated_at": generated_at,
                "data_sources_used": data_sources_used,
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


class AssetRequest:
    def __init__(self, env, headers, body=b""):
        self.scope = {"env": env}
        self.headers = headers
        self._body = body

    async def body(self):
        return self._body


class FakeObject:
    def __init__(self, content: bytes):
        self.content = content

    async def arrayBuffer(self):
        return self.content


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.get_calls = []

    async def put(self, key, body, **_kwargs):
        self.objects[key] = bytes(body)
        return type("Stored", (), {"httpEtag": '"asset-etag"'})()

    async def get(self, key, options=None):
        self.get_calls.append((key, options))
        content = self.objects.get(key)
        if content is None:
            return None
        if isinstance(options, dict) and isinstance(options.get("range"), dict):
            range_options = options["range"]
            offset = int(range_options["offset"])
            length = int(range_options["length"])
            content = content[offset : offset + length]
        return FakeObject(content)

    async def delete(self, key):
        self.objects.pop(key, None)


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


def test_asset_ranges_and_etag_matching_are_bounded():
    assert _parse_asset_range("bytes=1-3", 6) == (1, 3)
    assert _parse_asset_range("bytes=4-", 6) == (4, 5)
    assert _parse_asset_range("bytes=-2", 6) == (4, 5)
    assert _parse_asset_range(None, 6) is None
    assert _etag_matches('W/"asset-etag"', '"asset-etag"')
    assert _etag_matches("*", '"asset-etag"')
    for value in ("bytes=6-", "bytes=0-1,3-4", "items=0-1"):
        try:
            _parse_asset_range(value, 6)
        except ValueError:
            pass
        else:
            raise AssertionError(f"range should be rejected: {value}")


def test_asset_put_get_range_conditional_and_checksum_contract():
    secret = "asset-secret"
    encoded, signature = signed_context(secret)
    db = FakeDb()
    bucket = FakeBucket()
    env = SimpleNamespace(APP_DB=db, ASSETS=bucket, INTERNAL_ASSERTION_SECRET=secret)
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
        "content-type": "audio/wav",
    }
    payload = b"abcdef"
    checksum = hashlib.sha256(payload).hexdigest()
    put = asyncio.run(
        entry.put_asset(
            "audio/clip.wav",
            AssetRequest(env, {**headers, "x-content-sha256": checksum}, payload),
        )
    )
    assert put["checksum_sha256"] == checksum
    ranged = asyncio.run(
        entry.get_asset(
            "audio/clip.wav",
            AssetRequest(env, {**headers, "range": "bytes=1-3"}),
        )
    )
    assert ranged.status_code == 206
    assert ranged.body == b"bcd"
    assert ranged.headers["content-range"] == "bytes 1-3/6"
    assert ranged.headers["x-content-sha256"] == checksum
    not_modified = asyncio.run(
        entry.get_asset(
            "audio/clip.wav",
            AssetRequest(env, {**headers, "if-none-match": 'W/"asset-etag"'}),
        )
    )
    assert not_modified.status_code == 304
    rejected = asyncio.run(
        entry.put_asset(
            "audio/rejected.wav",
            AssetRequest(env, {**headers, "x-content-sha256": "0" * 64}, payload),
        )
    )
    assert rejected.status_code == 422


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


def test_api_keys_returns_only_configured_client_keys():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(
            INTERNAL_ASSERTION_SECRET=secret,
            FIREBASE_API_KEY="firebase-public",
            GOOGLE_CALENDAR_API_KEY="calendar-public",
        ),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
    )

    result = asyncio.run(entry.api_keys(request))

    assert result == {
        "firebase_api_key": "firebase-public",
        "google_calendar_api_key": "calendar-public",
    }


def test_api_keys_rejects_missing_auth():
    request = FakeRequest(SimpleNamespace(INTERNAL_ASSERTION_SECRET="test-secret"), {})

    response = asyncio.run(entry.api_keys(request))

    assert response.status_code == 401
    assert json.loads(response.body) == {"error": "unauthorized"}


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

    assert asyncio.run(entry.set_store_recording_permission(FakeRequest(env, headers, query={"value": "true"}))) == {
        "status": "ok"
    }
    assert asyncio.run(entry.set_private_cloud_sync(FakeRequest(env, headers, query={"value": "false"}))) == {
        "status": "ok"
    }
    assert asyncio.run(entry.get_store_recording_permission(FakeRequest(env, headers))) == {
        "store_recording_permission": True
    }
    assert asyncio.run(entry.get_private_cloud_sync(FakeRequest(env, headers))) == {"private_cloud_sync_enabled": False}

    invalid = asyncio.run(entry.set_private_cloud_sync(FakeRequest(env, headers, query={"value": "maybe"})))
    assert invalid.status_code == 400


def test_geolocation_is_uid_scoped_short_lived_and_preserves_success_contract(monkeypatch):
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="geolocation-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    database = FakeDb()
    env = SimpleNamespace(APP_DB=database, INTERNAL_ASSERTION_SECRET=secret)
    monkeypatch.setattr(entry.time, "time", lambda: 1_700_000_000)

    invalid = asyncio.run(entry.set_user_geolocation(FakeRequest(env, headers, {"latitude": 200, "longitude": 10})))
    assert invalid == {"status": "ok", "message": "Location ignored because its coordinates are invalid."}
    assert database.geolocation_row is None

    first = asyncio.run(
        entry.set_user_geolocation(
            FakeRequest(
                env,
                headers,
                {
                    "google_place_id": "place-1",
                    "latitude": 31.230416,
                    "longitude": 121.473701,
                    "address": "Shanghai",
                    "location_type": "locality",
                },
            )
        )
    )
    assert first == {"status": "ok"}
    assert database.geolocation_row["expires_at"] == 1_700_001_800

    unchanged = asyncio.run(
        entry.set_user_geolocation(FakeRequest(env, headers, {"latitude": 31.230419, "longitude": 121.473699}))
    )
    assert unchanged == {"status": "ok", "message": "Location not changed significantly."}

    changed = asyncio.run(
        entry.set_user_geolocation(FakeRequest(env, headers, {"latitude": 31.231, "longitude": 121.474}))
    )
    assert changed == {"status": "ok"}
    assert database.geolocation_row["latitude"] == 31.231

    monkeypatch.setattr(entry.time, "time", lambda: 1_700_001_801)
    assert asyncio.run(entry._load_geolocation(env, "geolocation-user")) is None
    assert database.geolocation_row is None


def test_training_data_opt_in_is_uid_scoped_and_enables_private_sync():
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="training-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    database = FakeDb()
    database.privacy_row = {
        "uid": "training-user",
        "store_recording_permission": 0,
        "private_cloud_sync_enabled": 0,
    }
    env = SimpleNamespace(APP_DB=database, INTERNAL_ASSERTION_SECRET=secret)

    assert asyncio.run(entry.get_training_data_opt_in(FakeRequest(env, headers))) == {
        "opted_in": False,
        "status": None,
    }
    submitted = asyncio.run(entry.set_training_data_opt_in(FakeRequest(env, headers)))
    assert submitted == {
        "status": "ok",
        "message": "Your request has been submitted for review. We will let you know soon.",
    }
    assert asyncio.run(entry.get_training_data_opt_in(FakeRequest(env, headers))) == {
        "opted_in": True,
        "status": "pending_review",
    }
    assert asyncio.run(entry.get_private_cloud_sync(FakeRequest(env, headers))) == {"private_cloud_sync_enabled": True}


def test_fcm_token_registration_scopes_and_sanitizes_device_key():
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="notification-token-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
        "x-app-platform": "iOS/17",
        "x-device-id-hash": "Device+Hash==",
    }
    database = FakeDb()
    env = SimpleNamespace(APP_DB=database, INTERNAL_ASSERTION_SECRET=secret)

    response = asyncio.run(
        entry.save_fcm_token(
            FakeRequest(
                env,
                headers,
                {"fcm_token": "fcm-token-value", "time_zone": "Asia/Shanghai"},
            )
        )
    )
    assert response == {"status": "Ok"}
    assert database.fcm_token_row == {
        "uid": "notification-token-user",
        "device_key": "ios17_devicehash",
        "token": "fcm-token-value",
        "time_zone": "Asia/Shanghai",
        "created_at": database.fcm_token_row["created_at"],
        "updated_at": database.fcm_token_row["updated_at"],
    }

    invalid = asyncio.run(
        entry.save_fcm_token(FakeRequest(env, headers, {"fcm_token": "", "time_zone": "Asia/Shanghai"}))
    )
    assert invalid.status_code == 400


def test_developer_webhook_configuration_and_toggle_state_round_trip():
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="webhook-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET=secret)

    assert asyncio.run(entry.get_developer_webhook(FakeRequest(env, headers), "realtime_transcript")) == {"url": ""}
    configured = asyncio.run(
        entry.set_developer_webhook(
            FakeRequest(env, headers, {"url": "https://webhook.example.test/transcript"}),
            "realtime_transcript",
        )
    )
    assert configured == {"status": "ok"}
    assert asyncio.run(entry.get_developer_webhook(FakeRequest(env, headers), "realtime_transcript")) == {
        "url": "https://webhook.example.test/transcript"
    }
    assert asyncio.run(entry.get_developer_webhooks_status(FakeRequest(env, headers))) == {
        "audio_bytes": False,
        "memory_created": False,
        "realtime_transcript": True,
        "day_summary": False,
    }

    assert asyncio.run(entry.disable_developer_webhook(FakeRequest(env, headers), "realtime_transcript")) == {
        "status": "ok"
    }
    assert asyncio.run(entry.enable_developer_webhook(FakeRequest(env, headers), "realtime_transcript")) == {
        "status": "ok"
    }

    assert asyncio.run(entry.set_developer_webhook(FakeRequest(env, headers, {"url": ",5"}), "audio_bytes")) == {
        "status": "ok"
    }
    assert asyncio.run(entry.get_developer_webhooks_status(FakeRequest(env, headers)))["audio_bytes"] is False

    invalid_type = asyncio.run(entry.get_developer_webhook(FakeRequest(env, headers), "unknown"))
    assert invalid_type.status_code == 400
    invalid_url = asyncio.run(
        entry.set_developer_webhook(FakeRequest(env, headers, {"url": "x" * 4_097}), "day_summary")
    )
    assert invalid_url.status_code == 400


def test_notification_settings_preserve_defaults_and_validate_frequency():
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="notifications-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET=secret)

    assert asyncio.run(entry.get_notification_settings(FakeRequest(env, headers))) == {"enabled": True, "frequency": 0}
    assert asyncio.run(
        entry.update_notification_settings(FakeRequest(env, headers, {"enabled": False, "frequency": 4}))
    ) == {"enabled": False, "frequency": 4}
    assert asyncio.run(entry.update_notification_settings(FakeRequest(env, headers, {"frequency": 2}))) == {
        "enabled": False,
        "frequency": 2,
    }
    invalid = asyncio.run(entry.update_notification_settings(FakeRequest(env, headers, {"frequency": 6})))
    assert invalid.status_code == 400


def test_daily_and_mentor_notification_preferences_round_trip_with_legacy_defaults():
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="notification-preferences-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET=secret)

    assert asyncio.run(entry.get_daily_summary_settings(FakeRequest(env, headers))) == {"enabled": True, "hour": 22}
    assert asyncio.run(entry.get_mentor_notification_settings(FakeRequest(env, headers))) == {"frequency": 0}

    assert asyncio.run(
        entry.update_daily_summary_settings(FakeRequest(env, headers, {"enabled": False, "hour": 0}))
    ) == {"status": "ok"}
    assert asyncio.run(entry.update_mentor_notification_settings(FakeRequest(env, headers, {"frequency": 5}))) == {
        "status": "ok"
    }
    assert asyncio.run(entry.get_daily_summary_settings(FakeRequest(env, headers))) == {"enabled": False, "hour": 0}
    assert asyncio.run(entry.get_mentor_notification_settings(FakeRequest(env, headers))) == {"frequency": 5}

    invalid_hour = asyncio.run(entry.update_daily_summary_settings(FakeRequest(env, headers, {"hour": 24})))
    assert invalid_hour.status_code == 400
    invalid_frequency = asyncio.run(
        entry.update_mentor_notification_settings(FakeRequest(env, headers, {"frequency": 6}))
    )
    assert invalid_frequency.status_code == 400


def test_assistant_settings_deep_merge_sections_and_preserve_uid_scope():
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="assistant-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET=secret)

    assert asyncio.run(entry.get_assistant_settings(FakeRequest(env, headers))) == {}
    first = asyncio.run(
        entry.update_assistant_settings(
            FakeRequest(
                env, headers, {"focus": {"enabled": True, "analysis_prompt": "focus"}, "update_channel": "beta"}
            )
        )
    )
    assert first == {"focus": {"enabled": True, "analysis_prompt": "focus"}, "update_channel": "beta"}

    second = asyncio.run(entry.update_assistant_settings(FakeRequest(env, headers, {"focus": {"enabled": False}})))
    assert second == {"focus": {"enabled": False, "analysis_prompt": "focus"}, "update_channel": "beta"}
    assert asyncio.run(entry.get_assistant_settings(FakeRequest(env, headers))) == second

    invalid = asyncio.run(entry.update_assistant_settings(FakeRequest(env, headers, {"focus": "not-an-object"})))
    assert invalid.status_code == 400


def test_ai_profile_partial_update_round_trips_and_rejects_oversized_text():
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="ai-profile-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET=secret)

    assert asyncio.run(entry.get_ai_profile(FakeRequest(env, headers))) is None
    updated = asyncio.run(
        entry.update_ai_profile(
            FakeRequest(env, headers, {"profile_text": "prefers concise answers", "data_sources_used": 3})
        )
    )
    assert updated == {"profile_text": "prefers concise answers", "data_sources_used": 3}
    assert asyncio.run(
        entry.update_ai_profile(FakeRequest(env, headers, {"generated_at": "2026-08-28T00:00:00Z"}))
    ) == {
        "profile_text": "prefers concise answers",
        "data_sources_used": 3,
        "generated_at": "2026-08-28T00:00:00Z",
    }

    oversized = asyncio.run(entry.update_ai_profile(FakeRequest(env, headers, {"profile_text": "x" * 50_001})))
    assert oversized.status_code == 400


def test_assistant_and_ai_profile_routes_fail_closed_without_auth():
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET="test-secret")
    for handler in (
        entry.get_assistant_settings,
        entry.update_assistant_settings,
        entry.get_ai_profile,
        entry.update_ai_profile,
        entry.get_training_data_opt_in,
        entry.set_training_data_opt_in,
        entry.save_fcm_token,
        lambda request: entry.set_developer_webhook(request, "realtime_transcript"),
        lambda request: entry.get_developer_webhook(request, "realtime_transcript"),
        lambda request: entry.disable_developer_webhook(request, "realtime_transcript"),
        lambda request: entry.enable_developer_webhook(request, "realtime_transcript"),
        entry.get_developer_webhooks_status,
        entry.get_daily_summary_settings,
        entry.update_daily_summary_settings,
        entry.get_mentor_notification_settings,
        entry.update_mentor_notification_settings,
        entry.set_user_geolocation,
    ):
        response = asyncio.run(handler(FakeRequest(env, {}, {})))
        assert response.status_code == 401


def test_location_context_consent_requires_disclosure_and_expires_after_thirty_days(monkeypatch):
    secret = "test-secret"
    encoded, signature = signed_context(secret, uid="location-user")
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
    }
    env = SimpleNamespace(APP_DB=FakeDb(), INTERNAL_ASSERTION_SECRET=secret)
    monkeypatch.setattr(entry.time, "time", lambda: 1_700_000_000)

    initial = asyncio.run(entry.get_location_context_consent(FakeRequest(env, headers)))
    assert initial["enabled"] is False
    assert initial["expires_at"] is None
    assert initial["purpose"] == "chat_city_context"

    rejected = asyncio.run(
        entry.set_location_context_consent(FakeRequest(env, headers, {"enabled": True, "disclosure_accepted": False}))
    )
    assert rejected.status_code == 422

    granted = asyncio.run(
        entry.set_location_context_consent(FakeRequest(env, headers, {"enabled": True, "disclosure_accepted": True}))
    )
    assert granted["enabled"] is True
    assert granted["expires_at"] == "2023-12-14T22:13:20+00:00"

    revoked = asyncio.run(entry.set_location_context_consent(FakeRequest(env, headers, {"enabled": False})))
    assert revoked["enabled"] is False
    assert revoked["expires_at"] is None
