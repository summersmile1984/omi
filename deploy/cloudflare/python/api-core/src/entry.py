import json
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

try:
    from workers import asgi, fetch as worker_fetch
except ModuleNotFoundError as error:  # CPython unit tests do not provide Pyodide's `js` module.
    if error.name != "js":
        raise
    asgi = None  # type: ignore[assignment]
    worker_fetch = None  # type: ignore[assignment]

from internal_auth import decode_context
from language_policy import (
    ACCEPTED_LANGUAGE_BASES,
    LANGUAGE_NAME_TO_BASE,
    MODULATE_SUPPORTED_LANGUAGES,
    PRIMARY_LANGUAGE_OPTIONS,
)
from firmware_policy import DEVICE_PREFIXES, FIRMWARE_TAG_PATTERN
from location_routes import (
    get_location_context_consent,
    router as location_router,
    set_location_context_consent,
)

app = FastAPI(title="Omi Cloudflare API Core", version="0.1.0")
MAX_ASSET_BODY_BYTES = 25_000_000
MAX_VOCABULARY_ITEMS = 100
MAX_ASSISTANT_SETTINGS_BYTES = 64_000
MAX_AI_PROFILE_TEXT_LENGTH = 50_000
MAX_FCM_TOKEN_LENGTH = 4_096
MAX_TIME_ZONE_LENGTH = 128
MAX_DEVICE_KEY_COMPONENT_LENGTH = 128
DEFAULT_DAILY_SUMMARY_HOUR_LOCAL = 22
DEFAULT_MENTOR_NOTIFICATION_FREQUENCY = 0


def auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-core", "version": "cf-02"}


@app.get("/v1/cf/probe")
async def probe(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    uid = str(context["uid"])
    await env.APP_DB.prepare(
        "INSERT INTO cf_worker_probe (uid, last_seen_at) VALUES (?, unixepoch()) "
        "ON CONFLICT(uid) DO UPDATE SET last_seen_at = excluded.last_seen_at"
    ).bind(uid).run()
    row = await env.APP_DB.prepare("SELECT uid, last_seen_at FROM cf_worker_probe WHERE uid = ?").bind(uid).first()
    return {"status": "ok", "service": "api-core", "auth": context, "probe": row}


@app.get("/v1/config/api-keys")
async def api_keys(request: Request) -> dict[str, str]:
    """Expose only explicitly configured client-facing API keys to an authed user."""
    if not auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    configured_keys = (
        ("firebase_api_key", "FIREBASE_API_KEY"),
        ("google_calendar_api_key", "GOOGLE_CALENDAR_API_KEY"),
        ("anthropic_api_key", "DESKTOP_LEGACY_ANTHROPIC_KEY"),
    )
    return {
        response_field: value
        for response_field, environment_name in configured_keys
        if (value := getattr(env, environment_name, None)) is not None
    }


class TranscriptionPreferencesUpdate(BaseModel):
    single_language_mode: bool | None = None
    vocabulary: list[str] | None = None


class UserLanguageUpdate(BaseModel):
    language: str


class OnboardingStateUpdate(BaseModel):
    completed: bool | None = None
    acquisition_source: str | None = None
    device_onboarding_completed: bool | None = None


class NotificationSettingsUpdate(BaseModel):
    enabled: bool | None = None
    frequency: int | None = Field(default=None, ge=0, le=5)


class DailySummarySettingsUpdate(BaseModel):
    enabled: bool | None = None
    hour: int | None = Field(default=None, ge=0, le=23)


class MentorNotificationSettingsUpdate(BaseModel):
    frequency: int = Field(ge=0, le=5)


class FcmTokenUpdate(BaseModel):
    fcm_token: str = Field(min_length=1, max_length=MAX_FCM_TOKEN_LENGTH)
    time_zone: str = Field(min_length=1, max_length=MAX_TIME_ZONE_LENGTH)


class AssistantSettingsUpdate(BaseModel):
    # The legacy contract accepts partial sections and ignores unknown
    # top-level fields. Keep section payloads JSON-shaped so D1 never stores
    # Python-specific values, while preserving forward-compatible settings.
    model_config = {"extra": "ignore"}

    shared: dict[str, object] | None = None
    focus: dict[str, object] | None = None
    task: dict[str, object] | None = None
    advice: dict[str, object] | None = None
    memory: dict[str, object] | None = None
    floating_bar: dict[str, object] | None = None
    web_search: dict[str, object] | None = None
    update_channel: str | None = Field(default=None, max_length=50)


class AIUserProfileUpdate(BaseModel):
    profile_text: str | None = Field(default=None, max_length=MAX_AI_PROFILE_TEXT_LENGTH)
    generated_at: str | None = Field(default=None, max_length=256)
    data_sources_used: int | None = Field(default=None, ge=0)


def _parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return None


def _transcription_preferences(row: object | None) -> dict[str, object]:
    if not isinstance(row, dict):
        return {
            "single_language_mode": False,
            "vocabulary": [],
            "language": "",
            "uses_custom_stt": False,
            "custom_stt_since": None,
        }
    try:
        vocabulary = json.loads(str(row.get("vocabulary_json") or "[]"))
    except (TypeError, ValueError):
        vocabulary = []
    if not isinstance(vocabulary, list) or not all(isinstance(item, str) for item in vocabulary):
        vocabulary = []
    return {
        "single_language_mode": bool(row.get("single_language_mode")),
        "vocabulary": vocabulary[:MAX_VOCABULARY_ITEMS],
        "language": str(row.get("language") or ""),
        "uses_custom_stt": bool(row.get("uses_custom_stt")),
        "custom_stt_since": row.get("custom_stt_since"),
    }


async def _load_transcription_preferences(env: object, uid: str) -> dict[str, object]:
    row = await env.APP_DB.prepare(
        "SELECT single_language_mode, vocabulary_json, language, uses_custom_stt, custom_stt_since "
        "FROM cf_user_transcription_preferences WHERE uid = ?"
    ).bind(uid).first()
    return _transcription_preferences(row)


def _normalize_user_language(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    base, separator, subtag = candidate.replace("_", "-").partition("-")
    normalized_base = base.lower()
    if normalized_base in ACCEPTED_LANGUAGE_BASES:
        if not separator:
            return normalized_base
        if subtag.isalnum() and 2 <= len(subtag) <= 4:
            return f"{normalized_base}-{subtag}"
    return LANGUAGE_NAME_TO_BASE.get(candidate.lower())


async def _save_transcription_preferences(env: object, uid: str, current: dict[str, object]) -> None:
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_transcription_preferences "
        "(uid, single_language_mode, vocabulary_json, language, uses_custom_stt, custom_stt_since, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET single_language_mode = excluded.single_language_mode, "
        "vocabulary_json = excluded.vocabulary_json, language = excluded.language, "
        "uses_custom_stt = excluded.uses_custom_stt, custom_stt_since = excluded.custom_stt_since, "
        "updated_at = excluded.updated_at"
    ).bind(
        uid,
        int(bool(current["single_language_mode"])),
        json.dumps(current["vocabulary"], ensure_ascii=False),
        str(current["language"]),
        int(bool(current["uses_custom_stt"])),
        current["custom_stt_since"],
        now,
        now,
    ).run()


def _onboarding_state(row: object | None) -> dict[str, object]:
    if not isinstance(row, dict):
        return {"completed": False, "acquisition_source": "", "device_onboarding_completed": False}
    return {
        "completed": bool(row.get("completed")),
        "acquisition_source": str(row.get("acquisition_source") or ""),
        "device_onboarding_completed": bool(row.get("device_onboarding_completed")),
    }


async def _load_onboarding_state(env: object, uid: str) -> dict[str, object]:
    row = await env.APP_DB.prepare(
        "SELECT completed, acquisition_source, device_onboarding_completed "
        "FROM cf_user_onboarding WHERE uid = ?"
    ).bind(uid).first()
    return _onboarding_state(row)


async def _save_onboarding_state(env: object, uid: str, state: dict[str, object]) -> None:
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_onboarding "
        "(uid, completed, acquisition_source, device_onboarding_completed, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET completed = excluded.completed, "
        "acquisition_source = excluded.acquisition_source, "
        "device_onboarding_completed = excluded.device_onboarding_completed, "
        "updated_at = excluded.updated_at"
    ).bind(
        uid,
        int(bool(state["completed"])),
        str(state["acquisition_source"]),
        int(bool(state["device_onboarding_completed"])),
        now,
        now,
    ).run()


def _privacy_settings(row: object | None) -> dict[str, object]:
    if not isinstance(row, dict):
        return {"store_recording_permission": False, "private_cloud_sync_enabled": True}
    return {
        "store_recording_permission": bool(row.get("store_recording_permission")),
        "private_cloud_sync_enabled": bool(row.get("private_cloud_sync_enabled"))
        if row.get("private_cloud_sync_enabled") is not None
        else True,
    }


async def _load_privacy_settings(env: object, uid: str) -> dict[str, object]:
    row = await env.APP_DB.prepare(
        "SELECT store_recording_permission, private_cloud_sync_enabled "
        "FROM cf_user_privacy_settings WHERE uid = ?"
    ).bind(uid).first()
    return _privacy_settings(row)


async def _save_privacy_settings(env: object, uid: str, settings: dict[str, object]) -> None:
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_privacy_settings "
        "(uid, store_recording_permission, private_cloud_sync_enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET store_recording_permission = excluded.store_recording_permission, "
        "private_cloud_sync_enabled = excluded.private_cloud_sync_enabled, updated_at = excluded.updated_at"
    ).bind(
        uid,
        int(bool(settings["store_recording_permission"])),
        int(bool(settings["private_cloud_sync_enabled"])),
        now,
        now,
    ).run()


def _training_data_opt_in(row: object | None) -> dict[str, object]:
    if not isinstance(row, dict):
        return {"opted_in": False, "status": None}
    status = row.get("status")
    if status not in {"pending_review", "approved", "rejected"}:
        return {"opted_in": False, "status": None}
    return {"opted_in": True, "status": status}


async def _load_training_data_opt_in(env: object, uid: str) -> dict[str, object]:
    row = await env.APP_DB.prepare(
        "SELECT status FROM cf_user_training_data_opt_in WHERE uid = ?"
    ).bind(uid).first()
    return _training_data_opt_in(row)


async def _save_training_data_opt_in(env: object, uid: str, status: str) -> None:
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_training_data_opt_in (uid, status, requested_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET status = excluded.status, requested_at = excluded.requested_at, "
        "updated_at = excluded.updated_at"
    ).bind(uid, status, now, now, now).run()


def _device_key_component(value: object, default: str) -> str:
    if not isinstance(value, str):
        return default
    normalized = re.sub(r"[^A-Za-z0-9._-]", "", value).lower()
    return normalized[:MAX_DEVICE_KEY_COMPONENT_LENGTH] or default


async def _save_fcm_token(env: object, uid: str, device_key: str, token: str, time_zone: str) -> None:
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_fcm_tokens (uid, device_key, token, time_zone, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid, device_key) DO UPDATE SET token = excluded.token, time_zone = excluded.time_zone, "
        "updated_at = excluded.updated_at"
    ).bind(uid, device_key, token, time_zone, now, now).run()


def _notification_settings(row: object | None) -> dict[str, object]:
    if not isinstance(row, dict):
        return {"enabled": True, "frequency": 0}
    raw_frequency = row.get("notification_frequency")
    frequency = raw_frequency if isinstance(raw_frequency, int) and 0 <= raw_frequency <= 5 else 0
    return {
        "enabled": bool(row.get("notifications_enabled"))
        if row.get("notifications_enabled") is not None
        else True,
        "frequency": frequency,
    }


async def _load_notification_settings(env: object, uid: str) -> dict[str, object]:
    row = await env.APP_DB.prepare(
        "SELECT notifications_enabled, notification_frequency "
        "FROM cf_user_notification_settings WHERE uid = ?"
    ).bind(uid).first()
    return _notification_settings(row)


async def _save_notification_settings(env: object, uid: str, settings: dict[str, object]) -> None:
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_notification_settings "
        "(uid, notifications_enabled, notification_frequency, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET notifications_enabled = excluded.notifications_enabled, "
        "notification_frequency = excluded.notification_frequency, updated_at = excluded.updated_at"
    ).bind(
        uid,
        int(bool(settings["enabled"])),
        int(settings["frequency"]),
        now,
        now,
    ).run()


def _daily_summary_settings(row: object | None) -> dict[str, object]:
    if not isinstance(row, dict):
        return {"enabled": True, "hour": DEFAULT_DAILY_SUMMARY_HOUR_LOCAL}
    raw_hour = row.get("daily_summary_hour_local")
    hour = raw_hour if isinstance(raw_hour, int) and 0 <= raw_hour <= 23 else DEFAULT_DAILY_SUMMARY_HOUR_LOCAL
    return {
        "enabled": bool(row.get("daily_summary_enabled"))
        if row.get("daily_summary_enabled") is not None
        else True,
        "hour": hour,
    }


def _mentor_notification_settings(row: object | None) -> dict[str, object]:
    if not isinstance(row, dict):
        return {"frequency": DEFAULT_MENTOR_NOTIFICATION_FREQUENCY}
    raw_frequency = row.get("mentor_notification_frequency")
    frequency = (
        raw_frequency
        if isinstance(raw_frequency, int) and 0 <= raw_frequency <= 5
        else DEFAULT_MENTOR_NOTIFICATION_FREQUENCY
    )
    return {"frequency": frequency}


async def _load_notification_preferences(env: object, uid: str) -> dict[str, object]:
    row = await env.APP_DB.prepare(
        "SELECT daily_summary_enabled, daily_summary_hour_local, mentor_notification_frequency "
        "FROM cf_user_notification_preferences WHERE uid = ?"
    ).bind(uid).first()
    return {
        "daily_summary_enabled": _daily_summary_settings(row)["enabled"],
        "daily_summary_hour_local": _daily_summary_settings(row)["hour"],
        "mentor_notification_frequency": _mentor_notification_settings(row)["frequency"],
    }


async def _save_notification_preferences(env: object, uid: str, settings: dict[str, object]) -> None:
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_notification_preferences "
        "(uid, daily_summary_enabled, daily_summary_hour_local, mentor_notification_frequency, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET daily_summary_enabled = excluded.daily_summary_enabled, "
        "daily_summary_hour_local = excluded.daily_summary_hour_local, "
        "mentor_notification_frequency = excluded.mentor_notification_frequency, updated_at = excluded.updated_at"
    ).bind(
        uid,
        int(bool(settings["daily_summary_enabled"])),
        int(settings["daily_summary_hour_local"]),
        int(settings["mentor_notification_frequency"]),
        now,
        now,
    ).run()


def _assistant_settings(row: object | None) -> dict[str, object]:
    if not isinstance(row, dict):
        return {}
    try:
        settings = json.loads(str(row.get("settings_json") or "{}"))
    except (TypeError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


async def _load_assistant_settings(env: object, uid: str) -> dict[str, object]:
    row = await env.APP_DB.prepare(
        "SELECT settings_json FROM cf_user_assistant_settings WHERE uid = ?"
    ).bind(uid).first()
    return _assistant_settings(row)


async def _save_assistant_settings(env: object, uid: str, settings: dict[str, object]) -> None:
    encoded = json.dumps(settings, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_ASSISTANT_SETTINGS_BYTES:
        raise ValueError("assistant settings exceed size limit")
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_assistant_settings (uid, settings_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(uid) DO UPDATE SET settings_json = excluded.settings_json, "
        "updated_at = excluded.updated_at"
    ).bind(uid, encoded, now, now).run()


def _merge_assistant_settings(existing: dict[str, object], update: dict[str, object]) -> dict[str, object]:
    merged = dict(existing)
    for key, value in update.items():
        if value is None:
            continue
        existing_section = merged.get(key)
        if isinstance(value, dict) and isinstance(existing_section, dict):
            merged[key] = {**existing_section, **value}
        else:
            merged[key] = value
    return merged


def _ai_profile(row: object | None) -> dict[str, object] | None:
    if not isinstance(row, dict):
        return None
    profile: dict[str, object] = {}
    if row.get("profile_text") is not None:
        profile["profile_text"] = str(row["profile_text"])
    if row.get("generated_at") is not None:
        profile["generated_at"] = str(row["generated_at"])
    if row.get("data_sources_used") is not None:
        profile["data_sources_used"] = int(row["data_sources_used"])
    return profile


async def _load_ai_profile(env: object, uid: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(
        "SELECT profile_text, generated_at, data_sources_used "
        "FROM cf_user_ai_profiles WHERE uid = ?"
    ).bind(uid).first()
    return _ai_profile(row)


async def _save_ai_profile(env: object, uid: str, profile: dict[str, object]) -> None:
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_ai_profiles "
        "(uid, profile_text, generated_at, data_sources_used, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET profile_text = excluded.profile_text, "
        "generated_at = excluded.generated_at, data_sources_used = excluded.data_sources_used, "
        "updated_at = excluded.updated_at"
    ).bind(
        uid,
        profile.get("profile_text"),
        profile.get("generated_at"),
        profile.get("data_sources_used"),
        now,
        now,
    ).run()


async def _query_bool(request: Request, name: str) -> bool | None:
    raw = request.query_params.get(name)
    if raw is not None:
        return _parse_bool(raw)
    try:
        body = await request.json()
    except (TypeError, ValueError):
        return None
    return _parse_bool(body)


async def _bounded_json(request: Request, max_bytes: int) -> object:
    body_reader = getattr(request, "body", None)
    if callable(body_reader):
        raw = await body_reader()
        if len(raw) > max_bytes:
            raise ValueError("request body exceeds size limit")
        return json.loads(raw)
    body = await request.json()
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > max_bytes:
        raise ValueError("request body exceeds size limit")
    return body


@app.get("/v1/users/available-languages")
async def available_languages(request: Request):
    if not auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"languages": [{"code": code, "name": name} for code, name in PRIMARY_LANGUAGE_OPTIONS]}


@app.get("/v1/users/onboarding")
async def get_onboarding_state(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _load_onboarding_state(request.scope["env"], str(context["uid"]))


@app.patch("/v1/users/onboarding")
async def update_onboarding_state(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = OnboardingStateUpdate.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid onboarding state"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    state = await _load_onboarding_state(env, uid)
    if update.completed is not None:
        state["completed"] = update.completed
    if update.acquisition_source is not None:
        state["acquisition_source"] = update.acquisition_source
    if update.device_onboarding_completed is not None:
        state["device_onboarding_completed"] = update.device_onboarding_completed
    await _save_onboarding_state(env, uid, state)
    return {"status": "ok"}


@app.get("/v1/users/store-recording-permission")
async def get_store_recording_permission(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    settings = await _load_privacy_settings(request.scope["env"], str(context["uid"]))
    return {"store_recording_permission": settings["store_recording_permission"]}


@app.post("/v1/users/store-recording-permission")
async def set_store_recording_permission(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    value = await _query_bool(request, "value")
    if value is None:
        return JSONResponse({"error": "invalid boolean value"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    settings = await _load_privacy_settings(env, uid)
    settings["store_recording_permission"] = value
    await _save_privacy_settings(env, uid, settings)
    return {"status": "ok"}


@app.get("/v1/users/private-cloud-sync")
async def get_private_cloud_sync(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    settings = await _load_privacy_settings(request.scope["env"], str(context["uid"]))
    return {"private_cloud_sync_enabled": settings["private_cloud_sync_enabled"]}


@app.post("/v1/users/private-cloud-sync")
async def set_private_cloud_sync(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    value = await _query_bool(request, "value")
    if value is None:
        return JSONResponse({"error": "invalid boolean value"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    settings = await _load_privacy_settings(env, uid)
    settings["private_cloud_sync_enabled"] = value
    await _save_privacy_settings(env, uid, settings)
    return {"status": "ok"}


@app.get("/v1/users/training-data-opt-in")
async def get_training_data_opt_in(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _load_training_data_opt_in(request.scope["env"], str(context["uid"]))


@app.post("/v1/users/training-data-opt-in")
async def set_training_data_opt_in(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    uid = str(context["uid"])
    await _save_training_data_opt_in(env, uid, "pending_review")

    # Opting in requires private sync. Notification delivery remains owned by
    # the legacy notifier until FCM token storage and delivery are migrated.
    privacy = await _load_privacy_settings(env, uid)
    if not privacy["private_cloud_sync_enabled"]:
        privacy["private_cloud_sync_enabled"] = True
        await _save_privacy_settings(env, uid, privacy)
    return {
        "status": "ok",
        "message": "Your request has been submitted for review. We will let you know soon.",
    }


@app.post("/v1/users/fcm-token")
async def save_fcm_token(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = FcmTokenUpdate.model_validate(await _bounded_json(request, 8_192))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid FCM token"}, status_code=400)
    platform = _device_key_component(request.headers.get("x-app-platform"), "unknown")
    device_hash = _device_key_component(request.headers.get("x-device-id-hash"), "default")
    device_key = f"{platform}_{device_hash}"
    await _save_fcm_token(request.scope["env"], str(context["uid"]), device_key, update.fcm_token, update.time_zone)
    return {"status": "Ok"}


@app.get("/v1/users/notification-settings")
async def get_notification_settings(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _load_notification_settings(request.scope["env"], str(context["uid"]))


@app.patch("/v1/users/notification-settings")
async def update_notification_settings(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = NotificationSettingsUpdate.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid notification settings"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    settings = await _load_notification_settings(env, uid)
    if update.enabled is not None:
        settings["enabled"] = update.enabled
    if update.frequency is not None:
        settings["frequency"] = update.frequency
    await _save_notification_settings(env, uid, settings)
    return await _load_notification_settings(env, uid)


@app.get("/v1/users/daily-summary-settings")
async def get_daily_summary_settings(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    settings = await _load_notification_preferences(request.scope["env"], str(context["uid"]))
    return {"enabled": settings["daily_summary_enabled"], "hour": settings["daily_summary_hour_local"]}


@app.patch("/v1/users/daily-summary-settings")
async def update_daily_summary_settings(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = DailySummarySettingsUpdate.model_validate(await _bounded_json(request, 4_000))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid daily summary settings"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    settings = await _load_notification_preferences(env, uid)
    if update.enabled is not None:
        settings["daily_summary_enabled"] = update.enabled
    if update.hour is not None:
        settings["daily_summary_hour_local"] = update.hour
    await _save_notification_preferences(env, uid, settings)
    return {"status": "ok"}


@app.get("/v1/users/mentor-notification-settings")
async def get_mentor_notification_settings(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    settings = await _load_notification_preferences(request.scope["env"], str(context["uid"]))
    return {"frequency": settings["mentor_notification_frequency"]}


@app.patch("/v1/users/mentor-notification-settings")
async def update_mentor_notification_settings(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = MentorNotificationSettingsUpdate.model_validate(await _bounded_json(request, 4_000))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid mentor notification settings"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    settings = await _load_notification_preferences(env, uid)
    settings["mentor_notification_frequency"] = update.frequency
    await _save_notification_preferences(env, uid, settings)
    return {"status": "ok"}


@app.get("/v1/users/assistant-settings")
async def get_assistant_settings(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _load_assistant_settings(request.scope["env"], str(context["uid"]))


@app.patch("/v1/users/assistant-settings")
async def update_assistant_settings(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = AssistantSettingsUpdate.model_validate(await _bounded_json(request, MAX_ASSISTANT_SETTINGS_BYTES))
        update_values = update.model_dump(exclude_unset=True)
        current = await _load_assistant_settings(request.scope["env"], str(context["uid"]))
        merged = _merge_assistant_settings(current, update_values)
        await _save_assistant_settings(request.scope["env"], str(context["uid"]), merged)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid assistant settings"}, status_code=400)
    return merged


@app.get("/v1/users/ai-profile")
async def get_ai_profile(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _load_ai_profile(request.scope["env"], str(context["uid"]))


@app.patch("/v1/users/ai-profile")
async def update_ai_profile(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = AIUserProfileUpdate.model_validate(await _bounded_json(request, MAX_ASSISTANT_SETTINGS_BYTES))
        values = update.model_dump(exclude_unset=True, exclude_none=True)
        current = await _load_ai_profile(request.scope["env"], str(context["uid"])) or {}
        current.update(values)
        await _save_ai_profile(request.scope["env"], str(context["uid"]), current)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid AI profile"}, status_code=400)
    return current


@app.get("/v1/users/language")
async def get_user_language(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    preferences = await _load_transcription_preferences(request.scope["env"], str(context["uid"]))
    return {"language": preferences["language"] or None}


@app.patch("/v1/users/language")
async def set_user_language(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = UserLanguageUpdate.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "A supported language code is required"}, status_code=400)
    language = _normalize_user_language(update.language)
    if not language:
        return JSONResponse({"error": "A supported language code is required"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    current = await _load_transcription_preferences(env, uid)
    current["language"] = language
    current["single_language_mode"] = language.split("-", 1)[0].lower() not in MODULATE_SUPPORTED_LANGUAGES
    await _save_transcription_preferences(env, uid, current)
    return {"status": "ok", "single_language_mode": current["single_language_mode"]}


@app.get("/v1/users/transcription-preferences")
async def get_transcription_preferences(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _load_transcription_preferences(request.scope["env"], str(context["uid"]))


@app.patch("/v1/users/transcription-preferences")
async def update_transcription_preferences(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = TranscriptionPreferencesUpdate.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid transcription preferences"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    current = await _load_transcription_preferences(env, uid)
    if update.single_language_mode is not None:
        current["single_language_mode"] = update.single_language_mode
    if update.vocabulary is not None:
        current["vocabulary"] = update.vocabulary[:MAX_VOCABULARY_ITEMS]
    await _save_transcription_preferences(env, uid, current)
    return {"status": "ok"}


app.include_router(location_router)


def _firmware_metadata(markdown: str) -> dict[str, object]:
    match = re.search(r"<!-- KEY_VALUE_START\s*(.*?)\s*KEY_VALUE_END -->", markdown or "", re.DOTALL)
    if not match:
        return {}
    result: dict[str, object] = {}
    for raw_line in match.group(1).splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key == "ota_update_steps":
            result[key] = [part.strip() for part in value.split(",") if part.strip()]
        elif key == "changelog":
            result[key] = [part.strip() for part in value.split("|") if part.strip()]
        else:
            result[key] = value
    return result


def _firmware_response(prefix: str, release: dict[str, object]) -> dict[str, object]:
    metadata = _firmware_metadata(str(release.get("body") or ""))
    assets = release.get("assets") if isinstance(release.get("assets"), list) else []
    suffix = ".bin" if prefix == "OmiGlass" else ".zip"
    asset = next(
        (
            item
            for item in assets
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].endswith(suffix)
            and (suffix == ".bin" or "ota" in item["name"].lower())
        ),
        None,
    )
    if not isinstance(asset, dict) or not asset.get("browser_download_url"):
        raise ValueError("firmware asset missing")
    return {
        "version": metadata.get("release_firmware_version"),
        "min_version": metadata.get("minimum_firmware_required"),
        "min_app_version": metadata.get("minimum_app_version"),
        "min_app_version_code": metadata.get("minimum_app_version_code"),
        "zip_url": asset["browser_download_url"],
        "draft": False,
        "ota_update_steps": metadata.get("ota_update_steps", []),
        "is_legacy_secure_dfu": str(metadata.get("is_legacy_secure_dfu", "True")).lower() == "true",
        "changelog": metadata.get("changelog", ""),
    }


def _parse_firmware_version(version: object) -> tuple[int, ...] | None:
    if not isinstance(version, str) or not version.strip():
        return None
    normalized = version.strip().lower()
    if normalized.startswith("v"):
        normalized = normalized[1:]
    parts = normalized.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    parsed = tuple(int(part) for part in parts)
    return parsed + (0,) * (3 - len(parsed))


def _firmware_candidates(releases: list[dict[str, object]], prefix: str) -> list[dict[str, object]]:
    return [
        release
        for release in releases
        if release.get("published_at")
        and not release.get("draft")
        and not release.get("prerelease")
        and isinstance(release.get("tag_name"), str)
        and FIRMWARE_TAG_PATTERN.fullmatch(str(release["tag_name"]))
        and str(release["tag_name"]).lower().startswith(prefix.lower() + "_v")
        and _parse_firmware_version(
            _firmware_metadata(str(release.get("body") or "")).get("release_firmware_version")
        )
    ]


async def _github_releases(env: object) -> list[dict[str, object]] | None:
    url = getattr(env, "FIRMWARE_RELEASES_URL", "https://api.github.com/repos/BasedHardware/omi/releases")
    headers = {
        "accept": "application/vnd.github+json",
        "user-agent": "omi-cloudflare-worker/0.1",
        "x-github-api-version": "2022-11-28",
    }
    token = getattr(env, "GITHUB_TOKEN", None)
    if token:
        headers["authorization"] = f"Bearer {token}"
    url = f"{url}{'&' if '?' in url else '?'}per_page=100"
    if worker_fetch is None:
        return None
    try:
        response = await worker_fetch(url, method="GET", headers=headers)
    except (OSError, TypeError, ValueError):
        return None
    if int(response.status) != 200:
        return None
    try:
        releases = await response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(releases, list):
        return None
    return [release for release in releases if isinstance(release, dict)]


def _firmware_upstream_error() -> JSONResponse:
    return JSONResponse({"error": "firmware upstream unavailable"}, status_code=502)


@app.get("/v2/firmware/stable")
async def firmware_stable(device_model: str, request: Request):
    prefix = DEVICE_PREFIXES.get(device_model)
    if not prefix:
        return JSONResponse({"error": "device not found"}, status_code=404)
    releases = await _github_releases(request.scope["env"])
    if releases is None:
        return _firmware_upstream_error()
    candidates = _firmware_candidates(releases, prefix)
    candidates.sort(key=lambda release: str(release.get("published_at") or ""), reverse=True)
    if not candidates:
        return JSONResponse({"error": "no stable firmware found"}, status_code=404)
    try:
        return _firmware_response(prefix, candidates[0])
    except ValueError:
        return JSONResponse({"error": "firmware asset missing"}, status_code=502)


@app.get("/v2/firmware/latest")
async def firmware_latest(
    device_model: str,
    firmware_revision: str,
    hardware_revision: str,
    manufacturer_name: str,
    request: Request,
):
    del hardware_revision, manufacturer_name
    prefix = DEVICE_PREFIXES.get(device_model)
    if not prefix:
        return JSONResponse({"error": "device not found"}, status_code=404)
    current = _parse_firmware_version(firmware_revision)
    if current is None:
        return JSONResponse({"error": "could not determine current firmware version"}, status_code=400)
    releases = await _github_releases(request.scope["env"])
    if releases is None:
        return _firmware_upstream_error()
    candidates = []
    for release in _firmware_candidates(releases, prefix):
        metadata = _firmware_metadata(str(release.get("body") or ""))
        release_version = _parse_firmware_version(metadata.get("release_firmware_version"))
        if release_version is None or release_version <= current:
            continue
        minimum = _parse_firmware_version(metadata.get("minimum_firmware_required"))
        if minimum is not None and current < minimum:
            continue
        candidates.append(release)
    candidates.sort(key=lambda release: str(release.get("published_at") or ""), reverse=True)
    if not candidates:
        return JSONResponse({"error": "no suitable firmware update found"}, status_code=404)
    try:
        return _firmware_response(prefix, candidates[0])
    except ValueError:
        return JSONResponse({"error": "firmware asset missing"}, status_code=502)


@app.get("/v2/firmware/version")
async def firmware_version(device_model: str, version: str, request: Request):
    prefix = DEVICE_PREFIXES.get(device_model)
    if not prefix:
        return JSONResponse({"error": "device not found"}, status_code=404)
    target = _parse_firmware_version(version)
    if target is None:
        return JSONResponse({"error": "could not parse requested firmware version"}, status_code=400)
    releases = await _github_releases(request.scope["env"])
    if releases is None:
        return _firmware_upstream_error()
    matches = [
        release
        for release in _firmware_candidates(releases, prefix)
        if _parse_firmware_version(
            _firmware_metadata(str(release.get("body") or "")).get("release_firmware_version")
        )
        == target
    ]
    matches.sort(key=lambda release: str(release.get("published_at") or ""), reverse=True)
    if not matches:
        return JSONResponse({"error": "requested firmware version not found"}, status_code=404)
    try:
        return _firmware_response(prefix, matches[0])
    except ValueError:
        return JSONResponse({"error": "firmware asset missing"}, status_code=502)


def _asset_key(uid: str, requested_key: str) -> str | None:
    key = requested_key.strip("/")
    if not key or len(key) > 512 or "\x00" in key:
        return None
    if any(part in {".", ".."} for part in key.split("/")):
        return None
    return f"{uid}/{key}"


def _asset_context(request: Request) -> tuple[dict[str, object] | None, object | None]:
    context = auth_context(request)
    if not context:
        return None, None
    env = request.scope["env"]
    if not getattr(env, "ASSETS", None):
        return context, None
    return context, env


@app.put("/v1/cf/assets/{requested_key:path}")
async def put_asset(requested_key: str, request: Request):
    context, env = _asset_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if env is None:
        return JSONResponse({"error": "asset storage is not configured"}, status_code=503)
    key = _asset_key(str(context["uid"]), requested_key)
    if not key:
        return JSONResponse({"error": "invalid asset key"}, status_code=400)
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_ASSET_BODY_BYTES:
        return JSONResponse({"error": "asset body too large"}, status_code=413)
    body = await request.body()
    if len(body) > MAX_ASSET_BODY_BYTES:
        return JSONResponse({"error": "asset body too large"}, status_code=413)
    content_type = request.headers.get("content-type", "application/octet-stream")[:200]
    stored = await env.ASSETS.put(key, body, httpMetadata={"contentType": content_type})
    etag = str(getattr(stored, "httpEtag", getattr(stored, "etag", "")))
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_asset_objects (uid, object_key, content_type, size, etag, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid, object_key) DO UPDATE SET content_type = excluded.content_type, "
        "size = excluded.size, etag = excluded.etag, updated_at = excluded.updated_at"
    ).bind(str(context["uid"]), key, content_type, len(body), etag, now, now).run()
    return {"status": "ok", "key": requested_key.strip("/"), "size": len(body), "etag": etag}


@app.get("/v1/cf/assets/{requested_key:path}")
async def get_asset(requested_key: str, request: Request):
    context, env = _asset_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if env is None:
        return JSONResponse({"error": "asset storage is not configured"}, status_code=503)
    key = _asset_key(str(context["uid"]), requested_key)
    if not key:
        return JSONResponse({"error": "invalid asset key"}, status_code=400)
    row = await env.APP_DB.prepare(
        "SELECT content_type, etag FROM cf_asset_objects WHERE uid = ? AND object_key = ?"
    ).bind(str(context["uid"]), key).first()
    if not row:
        return JSONResponse({"error": "asset not found"}, status_code=404)
    stored = await env.ASSETS.get(key)
    if not stored:
        return JSONResponse({"error": "asset not found"}, status_code=404)
    content = bytes(await stored.arrayBuffer())
    return Response(
        content=content,
        media_type=str(row["content_type"]),
        headers={"etag": str(row["etag"]), "content-length": str(len(content))},
    )


@app.delete("/v1/cf/assets/{requested_key:path}")
async def delete_asset(requested_key: str, request: Request):
    context, env = _asset_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if env is None:
        return JSONResponse({"error": "asset storage is not configured"}, status_code=503)
    key = _asset_key(str(context["uid"]), requested_key)
    if not key:
        return JSONResponse({"error": "invalid asset key"}, status_code=400)
    await env.ASSETS.delete(key)
    await env.APP_DB.prepare("DELETE FROM cf_asset_objects WHERE uid = ? AND object_key = ?").bind(
        str(context["uid"]), key
    ).run()
    return {"status": "deleted", "key": requested_key.strip("/")}


Default = asgi.entrypoint(app) if asgi else None
