import hashlib
import json
import math
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

try:
    from workers import asgi, fetch as worker_fetch
except ModuleNotFoundError as error:  # CPython unit tests do not provide Pyodide's `js` module.
    if error.name != "js":
        raise
    asgi = None  # type: ignore[assignment]
    worker_fetch = None  # type: ignore[assignment]

from internal_auth import decode_context, verify_request_context
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
from action_item_routes import batch_router as action_item_batch_router
from action_item_routes import router as action_item_router
from account_routes import router as account_router
from fair_use_routes import router as fair_use_router
from people_routes import router as people_router
from goal_routes import router as goal_router
from folder_routes import router as folder_router
from score_routes import router as score_router
from focus_routes import router as focus_router
from advice_routes import router as advice_router
from screen_activity_routes import router as screen_activity_router
from calendar_onboarding_routes import router as calendar_onboarding_router
from calendar_meeting_routes import router as calendar_meeting_router
from apple_health_routes import router as apple_health_router
from trend_routes import router as trend_router
from workstream_routes import router as workstream_router
from announcement_routes import router as announcement_router
from conversation_routes import router as conversation_router
from public_shared_chat_routes import router as public_shared_chat_router
from conversation_finalization_routes import router as conversation_finalization_router
from conversation_merge_routes import router as conversation_merge_router
from account_cutover_routes import router as account_cutover_router
from app_catalog_routes import router as app_catalog_router
from app_projection_routes import router as app_projection_router
from app_install_routes import router as app_install_router
from app_catalog_v2_routes import router as app_catalog_v2_router
from memory_routes import router as memory_router
from memory_review_routes import router as memory_review_router
from memory_import_routes import router as memory_import_router
from limitless_import_routes import router as limitless_import_router
from daily_summary_routes import router as daily_summary_router
from chat_routes import router as chat_router
from chat_session_routes import router as chat_session_router
from app_review_routes import router as app_review_router
from feedback_routes import router as feedback_router
from llm_usage_routes import router as llm_usage_router
from overage_routes import router as overage_router
from payment_callback_routes import router as payment_callback_router
from integration_routes import router as integration_router
from mcp_routes import router as mcp_router
from developer_routes import router as developer_router
from developer_mutation_routes import router as developer_mutation_router
from developer_conversation_create_routes import router as developer_conversation_create_router
from tool_routes import router as tool_router
from agent_tools_routes import router as agent_tools_router
from knowledge_graph_routes import router as knowledge_graph_router
from synthesis_routes import router as synthesis_router
from goal_ai_routes import router as goal_ai_router
from speech_profile_routes import router as speech_profile_router
from user_export_routes import router as user_export_router
from retired_compat_routes import router as retired_compat_router
from chat_first_routes import router as chat_first_router
from crisp_routes import router as crisp_router
from migration_routes import router as migration_router
from candidate_control_routes import router as candidate_control_router
from candidate_compat_routes import router as candidate_compat_router
from desktop_release_routes import router as desktop_release_router
from desktop_beta_routes import router as desktop_beta_router
from followup_routes import router as followup_router
from persona_routes import router as persona_router
from sentry_routes import router as sentry_router
from conversation_test_prompt_routes import router as conversation_test_prompt_router

app = FastAPI(title="Omi Cloudflare API Core", version="0.1.0")
app.include_router(score_router)
app.include_router(focus_router)
app.include_router(advice_router)
app.include_router(screen_activity_router)
app.include_router(calendar_onboarding_router)
app.include_router(calendar_meeting_router)
app.include_router(apple_health_router)
app.include_router(trend_router)
app.include_router(workstream_router)
app.include_router(announcement_router)
app.include_router(conversation_router)
app.include_router(public_shared_chat_router)
app.include_router(conversation_finalization_router)
app.include_router(conversation_merge_router)
app.include_router(account_cutover_router)
app.include_router(app_catalog_router)
app.include_router(app_install_router)
app.include_router(app_projection_router)
app.include_router(app_catalog_v2_router)
app.include_router(memory_router)
app.include_router(memory_review_router)
app.include_router(memory_import_router)
app.include_router(limitless_import_router)
app.include_router(daily_summary_router)
app.include_router(chat_router)
app.include_router(chat_session_router)
app.include_router(app_review_router)
app.include_router(feedback_router)
app.include_router(llm_usage_router)
app.include_router(overage_router)
app.include_router(payment_callback_router)
app.include_router(integration_router)
app.include_router(mcp_router)
app.include_router(developer_router)
app.include_router(developer_mutation_router)
app.include_router(developer_conversation_create_router)
app.include_router(tool_router)
app.include_router(agent_tools_router)
app.include_router(knowledge_graph_router)
app.include_router(synthesis_router)
app.include_router(goal_ai_router)
app.include_router(speech_profile_router)
app.include_router(user_export_router)
app.include_router(retired_compat_router)
app.include_router(chat_first_router)
app.include_router(crisp_router)
app.include_router(migration_router)
app.include_router(candidate_control_router)
app.include_router(candidate_compat_router)
app.include_router(desktop_release_router)
app.include_router(desktop_beta_router)
app.include_router(followup_router)
app.include_router(persona_router)
app.include_router(sentry_router)
app.include_router(conversation_test_prompt_router)
MAX_ASSET_BODY_BYTES = 25_000_000
ASSET_CLEANUP_GRACE_SECONDS = 15 * 60
ASSET_CLEANUP_BATCH_SIZE = 10
MAX_VOCABULARY_ITEMS = 100
MAX_ASSISTANT_SETTINGS_BYTES = 64_000
MAX_AI_PROFILE_TEXT_LENGTH = 50_000
MAX_FCM_TOKEN_LENGTH = 4_096
MAX_TIME_ZONE_LENGTH = 128
MAX_DEVICE_KEY_COMPONENT_LENGTH = 128
MAX_WEBHOOK_URL_LENGTH = 4_096
MAX_GOOGLE_PLACE_ID_LENGTH = 512
MAX_GEOLOCATION_ADDRESS_LENGTH = 2_048
MAX_LOCATION_TYPE_LENGTH = 128
GEOLOCATION_TTL_SECONDS = 30 * 60
DEFAULT_DAILY_SUMMARY_HOUR_LOCAL = 22
DEFAULT_MENTOR_NOTIFICATION_FREQUENCY = 0
WEBHOOK_TYPES = frozenset(
    {
        "audio_bytes",
        "audio_bytes_websocket",
        "realtime_transcript",
        "memory_created",
        "day_summary",
    }
)


def auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


@app.middleware("http")
async def enforce_request_bound_auth_context(request: Request, call_next):
    encoded = request.headers.get("x-omi-auth-context")
    signature = request.headers.get("x-omi-internal-signature")
    if encoded or signature:
        env = request.scope.get("env")
        context = verify_request_context(
            encoded,
            signature,
            getattr(env, "INTERNAL_ASSERTION_SECRET", None),
            audience="api-core",
            method=request.method,
            path=request.url.path,
        )
        if context is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        request.state.auth_context = context
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-core", "version": "cf-02"}


@app.get("/")
async def root() -> dict[str, str]:
    """Expose the API Core health payload at the legacy root path."""
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


class DeveloperWebhookUpdate(BaseModel):
    url: str = Field(max_length=MAX_WEBHOOK_URL_LENGTH)


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


class GeolocationUpdate(BaseModel):
    google_place_id: str | None = Field(default=None, max_length=MAX_GOOGLE_PLACE_ID_LENGTH)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    address: str | None = Field(default=None, max_length=MAX_GEOLOCATION_ADDRESS_LENGTH)
    location_type: str | None = Field(default=None, max_length=MAX_LOCATION_TYPE_LENGTH)


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
    row = (
        await env.APP_DB.prepare(
            "SELECT single_language_mode, vocabulary_json, language, uses_custom_stt, custom_stt_since "
            "FROM cf_user_transcription_preferences WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
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
    row = (
        await env.APP_DB.prepare(
            "SELECT completed, acquisition_source, device_onboarding_completed " "FROM cf_user_onboarding WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
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
        "private_cloud_sync_enabled": (
            bool(row.get("private_cloud_sync_enabled")) if row.get("private_cloud_sync_enabled") is not None else True
        ),
    }


async def _load_privacy_settings(env: object, uid: str) -> dict[str, object]:
    row = (
        await env.APP_DB.prepare(
            "SELECT store_recording_permission, private_cloud_sync_enabled "
            "FROM cf_user_privacy_settings WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
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


async def _recording_deletion_active(env: object, uid: str) -> bool:
    row = (
        await env.APP_DB.prepare("SELECT 1 AS active FROM cf_recording_deletion_intents WHERE uid = ? LIMIT 1")
        .bind(uid)
        .first()
    )
    return isinstance(row, dict) and bool(row.get("active"))


async def _recording_storage_enabled(env: object, uid: str) -> bool:
    settings = await _load_privacy_settings(env, uid)
    return bool(settings["store_recording_permission"]) and not await _recording_deletion_active(env, uid)


def _training_data_opt_in(row: object | None) -> dict[str, object]:
    if not isinstance(row, dict):
        return {"opted_in": False, "status": None}
    status = row.get("status")
    if status not in {"pending_review", "approved", "rejected"}:
        return {"opted_in": False, "status": None}
    return {"opted_in": True, "status": status}


async def _load_training_data_opt_in(env: object, uid: str) -> dict[str, object]:
    row = await env.APP_DB.prepare("SELECT status FROM cf_user_training_data_opt_in WHERE uid = ?").bind(uid).first()
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


async def _load_geolocation(env: object, uid: str, *, now: int | None = None) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT google_place_id, latitude, longitude, address, location_type, updated_at, expires_at "
            "FROM cf_user_geolocation WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
    if not isinstance(row, dict):
        return None
    current_time = int(time.time()) if now is None else now
    try:
        expires_at = int(row["expires_at"])
    except (KeyError, TypeError, ValueError):
        expires_at = 0
    if expires_at <= current_time:
        await env.APP_DB.prepare("DELETE FROM cf_user_geolocation WHERE uid = ?").bind(uid).run()
        return None
    return row


async def _save_geolocation(env: object, uid: str, update: GeolocationUpdate, *, now: int | None = None) -> None:
    current_time = int(time.time()) if now is None else now
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_geolocation "
        "(uid, google_place_id, latitude, longitude, address, location_type, updated_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET google_place_id = excluded.google_place_id, "
        "latitude = excluded.latitude, longitude = excluded.longitude, address = excluded.address, "
        "location_type = excluded.location_type, updated_at = excluded.updated_at, expires_at = excluded.expires_at"
    ).bind(
        uid,
        update.google_place_id,
        update.latitude,
        update.longitude,
        update.address,
        update.location_type,
        current_time,
        current_time + GEOLOCATION_TTL_SECONDS,
    ).run()


def _valid_webhook_type(value: str) -> bool:
    return value in WEBHOOK_TYPES


def _webhook_is_configured(webhook_type: str, url: str) -> bool:
    candidate = url.split(",", 1)[0] if webhook_type == "audio_bytes" else url
    return bool(candidate.strip())


async def _load_developer_webhook(env: object, uid: str, webhook_type: str) -> dict[str, object]:
    row = (
        await env.APP_DB.prepare(
            "SELECT url, enabled FROM cf_user_developer_webhooks WHERE uid = ? AND webhook_type = ?"
        )
        .bind(uid, webhook_type)
        .first()
    )
    if not isinstance(row, dict):
        return {"url": "", "enabled": False}
    url = str(row.get("url") or "")
    return {"url": url, "enabled": bool(row.get("enabled")) and _webhook_is_configured(webhook_type, url)}


async def _save_developer_webhook(
    env: object,
    uid: str,
    webhook_type: str,
    url: str,
    enabled: bool,
) -> None:
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_developer_webhooks "
        "(uid, webhook_type, url, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid, webhook_type) DO UPDATE SET url = excluded.url, enabled = excluded.enabled, "
        "updated_at = excluded.updated_at"
    ).bind(uid, webhook_type, url, int(enabled), now, now).run()


def _notification_settings(row: object | None) -> dict[str, object]:
    if not isinstance(row, dict):
        return {"enabled": True, "frequency": 0}
    raw_frequency = row.get("notification_frequency")
    frequency = raw_frequency if isinstance(raw_frequency, int) and 0 <= raw_frequency <= 5 else 0
    return {
        "enabled": bool(row.get("notifications_enabled")) if row.get("notifications_enabled") is not None else True,
        "frequency": frequency,
    }


async def _load_notification_settings(env: object, uid: str) -> dict[str, object]:
    row = (
        await env.APP_DB.prepare(
            "SELECT notifications_enabled, notification_frequency " "FROM cf_user_notification_settings WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
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
        "enabled": bool(row.get("daily_summary_enabled")) if row.get("daily_summary_enabled") is not None else True,
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
    row = (
        await env.APP_DB.prepare(
            "SELECT daily_summary_enabled, daily_summary_hour_local, mentor_notification_frequency "
            "FROM cf_user_notification_preferences WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
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
    row = (
        await env.APP_DB.prepare("SELECT settings_json FROM cf_user_assistant_settings WHERE uid = ?").bind(uid).first()
    )
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
    row = (
        await env.APP_DB.prepare(
            "SELECT profile_text, generated_at, data_sources_used " "FROM cf_user_ai_profiles WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
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
    if value and await _recording_deletion_active(env, uid):
        return JSONResponse({"error": "recording deletion in progress"}, status_code=409)
    settings = await _load_privacy_settings(env, uid)
    settings["store_recording_permission"] = value
    try:
        await _save_privacy_settings(env, uid, settings)
    except Exception:
        if value and await _recording_deletion_active(env, uid):
            return JSONResponse({"error": "recording deletion in progress"}, status_code=409)
        return JSONResponse({"error": "privacy settings unavailable"}, status_code=503)
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


@app.patch("/v1/users/geolocation")
async def set_user_geolocation(request: Request):
    """Store a short-lived, uid-scoped location used by the chat context seam.

    The legacy route uses a Redis key with a 30-minute TTL and returns a
    success-shaped response for invalid coordinates. Keep that wire behavior,
    but make the Worker authority explicit in D1 and never persist malformed
    or non-finite coordinates.
    """
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = GeolocationUpdate.model_validate(await _bounded_json(request, 8_192))
        if not math.isfinite(update.latitude) or not math.isfinite(update.longitude):
            raise ValueError("coordinates must be finite")
    except (ValidationError, ValueError, TypeError):
        return {"status": "ok", "message": "Location ignored because its coordinates are invalid."}

    env = request.scope["env"]
    uid = str(context["uid"])
    current = await _load_geolocation(env, uid)
    if current:
        try:
            if round(float(current["latitude"]), 4) == round(update.latitude, 4) and round(
                float(current["longitude"]), 4
            ) == round(update.longitude, 4):
                return {"status": "ok", "message": "Location not changed significantly."}
        except (KeyError, TypeError, ValueError):
            pass
    await _save_geolocation(env, uid, update)
    return {"status": "ok"}


@app.post("/v1/users/developer/webhook/{wtype}")
async def set_developer_webhook(request: Request, wtype: str):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_webhook_type(wtype):
        return JSONResponse({"error": "invalid webhook type"}, status_code=400)
    try:
        update = DeveloperWebhookUpdate.model_validate(await _bounded_json(request, 8_192))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid webhook"}, status_code=400)
    url = update.url
    await _save_developer_webhook(
        request.scope["env"],
        str(context["uid"]),
        wtype,
        url,
        _webhook_is_configured(wtype, url),
    )
    return {"status": "ok"}


@app.get("/v1/users/developer/webhook/{wtype}")
async def get_developer_webhook(request: Request, wtype: str):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_webhook_type(wtype):
        return JSONResponse({"error": "invalid webhook type"}, status_code=400)
    row = await _load_developer_webhook(request.scope["env"], str(context["uid"]), wtype)
    return {"url": row["url"]}


@app.post("/v1/users/developer/webhook/{wtype}/disable")
async def disable_developer_webhook(request: Request, wtype: str):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_webhook_type(wtype):
        return JSONResponse({"error": "invalid webhook type"}, status_code=400)
    row = await _load_developer_webhook(request.scope["env"], str(context["uid"]), wtype)
    await _save_developer_webhook(request.scope["env"], str(context["uid"]), wtype, str(row["url"]), False)
    return {"status": "ok"}


@app.post("/v1/users/developer/webhook/{wtype}/enable")
async def enable_developer_webhook(request: Request, wtype: str):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_webhook_type(wtype):
        return JSONResponse({"error": "invalid webhook type"}, status_code=400)
    row = await _load_developer_webhook(request.scope["env"], str(context["uid"]), wtype)
    enabled = _webhook_is_configured(wtype, str(row["url"]))
    await _save_developer_webhook(request.scope["env"], str(context["uid"]), wtype, str(row["url"]), enabled)
    return {"status": "ok"}


@app.get("/v1/users/developer/webhooks/status")
async def get_developer_webhooks_status(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    uid = str(context["uid"])
    return {
        webhook_type: (await _load_developer_webhook(env, uid, webhook_type))["enabled"]
        for webhook_type in ("audio_bytes", "memory_created", "realtime_transcript", "day_summary")
    }


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
app.include_router(action_item_batch_router)
app.include_router(action_item_router)
app.include_router(account_router)
app.include_router(fair_use_router)
app.include_router(people_router)
app.include_router(goal_router)
app.include_router(folder_router)


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
        and _parse_firmware_version(_firmware_metadata(str(release.get("body") or "")).get("release_firmware_version"))
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
        if _parse_firmware_version(_firmware_metadata(str(release.get("body") or "")).get("release_firmware_version"))
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


def _parse_asset_range(raw: str | None, size: int) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range against the D1 metadata size.

    R2 supports a single range efficiently. Multiple ranges would require a
    multipart response assembled in the Worker, which would defeat the object
    streaming boundary and is intentionally rejected.
    """

    if not raw:
        return None
    if not raw.startswith("bytes=") or "," in raw:
        raise ValueError("only one bytes range is supported")
    spec = raw[6:].strip()
    if "-" not in spec:
        raise ValueError("malformed bytes range")
    start_raw, end_raw = spec.split("-", 1)
    if not start_raw and not end_raw:
        raise ValueError("malformed bytes range")
    if size <= 0:
        raise ValueError("empty object has no satisfiable range")
    try:
        if not start_raw:
            suffix = int(end_raw)
            if suffix <= 0:
                raise ValueError("invalid suffix")
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_raw)
            if start < 0 or start >= size:
                raise ValueError("range starts past object")
            end = size - 1 if not end_raw else min(size - 1, int(end_raw))
            if end < start:
                raise ValueError("range end precedes start")
    except (TypeError, ValueError) as error:
        raise ValueError("unsatisfiable bytes range") from error
    return start, end


def _etag_matches(raw: str | None, etag: str) -> bool:
    if not raw:
        return False
    candidate = etag.strip()
    if not candidate:
        return False
    for item in raw.split(","):
        normalized = item.strip()
        if normalized == "*":
            return True
        if normalized.startswith("W/"):
            normalized = normalized[2:]
        if normalized.strip('"') == candidate.strip('"'):
            return True
    return False


def _asset_storage_key(uid: str, checksum: str) -> str:
    return f"cf-assets/{uid}/{checksum}/{uuid.uuid4().hex}"


async def _read_bounded_asset_body(request: Request) -> tuple[bytes, str] | None:
    body = bytearray()
    digest = hashlib.sha256()
    async for chunk in request.stream():
        data = bytes(chunk)
        if len(body) + len(data) > MAX_ASSET_BODY_BYTES:
            return None
        body.extend(data)
        digest.update(data)
    return bytes(body), digest.hexdigest()


async def _r2_body_chunks(stored: object):
    stream = getattr(stored, "body", None)
    get_reader = getattr(stream, "getReader", None)
    if callable(get_reader):
        reader = get_reader()
        try:
            while True:
                result = await reader.read()
                if bool(getattr(result, "done", False)):
                    break
                value = getattr(result, "value", b"")
                to_py = getattr(value, "to_py", None)
                yield bytes(to_py() if callable(to_py) else value)
        finally:
            release_lock = getattr(reader, "releaseLock", None)
            if callable(release_lock):
                release_lock()
        return
    yield bytes(await stored.arrayBuffer())


async def _drain_asset_cleanup(env: object, uid: str, logical_key: str) -> None:
    now = int(time.time())
    result = (
        await env.APP_DB.prepare(
            "SELECT storage_key FROM cf_asset_cleanup_tasks "
            "WHERE uid = ? AND logical_key = ? AND not_before <= ? ORDER BY created_at LIMIT ?"
        )
        .bind(uid, logical_key, now, ASSET_CLEANUP_BATCH_SIZE)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("storage_key"), str):
            continue
        storage_key = str(row["storage_key"])
        active = (
            await env.APP_DB.prepare(
                "SELECT 1 AS active FROM cf_asset_objects WHERE uid = ? AND storage_key = ? LIMIT 1"
            )
            .bind(uid, storage_key)
            .first()
        )
        if not active:
            try:
                await env.ASSETS.delete(storage_key)
            except Exception:
                await env.APP_DB.prepare(
                    "UPDATE cf_asset_cleanup_tasks SET attempts = attempts + 1, last_error = ?, updated_at = ? "
                    "WHERE storage_key = ? AND uid = ?"
                ).bind("r2 delete unavailable", now, storage_key, uid).run()
                continue
        await env.APP_DB.prepare("DELETE FROM cf_asset_cleanup_tasks WHERE storage_key = ? AND uid = ?").bind(
            storage_key, uid
        ).run()


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
    content_type = request.headers.get("content-type", "application/octet-stream")[:200]
    uid = str(context["uid"])
    if content_type.startswith("audio/") and not await _recording_storage_enabled(env, uid):
        return JSONResponse({"error": "recording storage is disabled"}, status_code=409)
    bounded_body = await _read_bounded_asset_body(request)
    if bounded_body is None:
        return JSONResponse({"error": "asset body too large"}, status_code=413)
    body, checksum = bounded_body
    expected_checksum = request.headers.get("x-content-sha256")
    if expected_checksum and expected_checksum.strip().lower() != checksum:
        return JSONResponse({"error": "asset checksum mismatch"}, status_code=422)
    storage_key = _asset_storage_key(uid, checksum)
    now = int(time.time())
    try:
        await env.APP_DB.prepare(
            "INSERT INTO cf_asset_cleanup_tasks "
            "(storage_key, uid, logical_key, content_type, reason, not_before, attempts, last_error, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'uncommitted-upload', ?, 0, NULL, ?, ?)"
        ).bind(storage_key, uid, key, content_type, now + ASSET_CLEANUP_GRACE_SECONDS, now, now).run()
    except Exception:
        return JSONResponse({"error": "asset metadata is unavailable"}, status_code=503)
    try:
        stored = await env.ASSETS.put(storage_key, body, httpMetadata={"contentType": content_type})
    except Exception:
        try:
            await env.APP_DB.prepare("DELETE FROM cf_asset_cleanup_tasks WHERE storage_key = ? AND uid = ?").bind(
                storage_key, uid
            ).run()
        except Exception:
            pass
        return JSONResponse({"error": "asset storage is unavailable"}, status_code=503)
    etag = str(getattr(stored, "httpEtag", getattr(stored, "etag", "")))
    cleanup_previous = env.APP_DB.prepare(
        "INSERT OR IGNORE INTO cf_asset_cleanup_tasks "
        "(storage_key, uid, logical_key, content_type, reason, not_before, attempts, last_error, "
        "created_at, updated_at) "
        "SELECT storage_key, uid, object_key, content_type, 'superseded', ?, 0, NULL, ?, ? FROM cf_asset_objects "
        "WHERE uid = ? AND object_key = ? AND storage_key IS NOT NULL AND storage_key <> ?"
    ).bind(now, now, now, uid, key, storage_key)
    upsert_metadata = env.APP_DB.prepare(
        "INSERT INTO cf_asset_objects "
        "(uid, object_key, storage_key, content_type, size, etag, checksum_sha256, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid, object_key) DO UPDATE SET storage_key = excluded.storage_key, "
        "content_type = excluded.content_type, size = excluded.size, etag = excluded.etag, "
        "checksum_sha256 = excluded.checksum_sha256, updated_at = excluded.updated_at"
    ).bind(uid, key, storage_key, content_type, len(body), etag, checksum, now, now)
    commit_intent = env.APP_DB.prepare("DELETE FROM cf_asset_cleanup_tasks WHERE storage_key = ? AND uid = ?").bind(
        storage_key, uid
    )
    try:
        await env.APP_DB.batch([cleanup_previous, upsert_metadata, commit_intent])
    except Exception:
        try:
            active = (
                await env.APP_DB.prepare("SELECT storage_key FROM cf_asset_objects WHERE uid = ? AND object_key = ?")
                .bind(uid, key)
                .first()
            )
        except Exception:
            active = None
        if not isinstance(active, dict) or active.get("storage_key") != storage_key:
            try:
                await env.ASSETS.delete(storage_key)
                await env.APP_DB.prepare("DELETE FROM cf_asset_cleanup_tasks WHERE storage_key = ? AND uid = ?").bind(
                    storage_key, uid
                ).run()
            except Exception:
                pass
            return JSONResponse({"error": "asset metadata is unavailable"}, status_code=503)
    try:
        await _drain_asset_cleanup(env, uid, key)
    except Exception:
        pass
    return {
        "status": "ok",
        "key": requested_key.strip("/"),
        "size": len(body),
        "etag": etag,
        "checksum_sha256": checksum,
    }


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
    row = (
        await env.APP_DB.prepare(
            "SELECT storage_key, content_type, etag, size, checksum_sha256 "
            "FROM cf_asset_objects WHERE uid = ? AND object_key = ?"
        )
        .bind(str(context["uid"]), key)
        .first()
    )
    if not row:
        return JSONResponse({"error": "asset not found"}, status_code=404)
    if str(row.get("content_type") or "").startswith("audio/") and not await _recording_storage_enabled(
        env, str(context["uid"])
    ):
        return JSONResponse({"error": "asset not found"}, status_code=404)
    etag = str(row.get("etag") or "")
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"etag": etag})
    size = int(row.get("size") or 0)
    try:
        byte_range = _parse_asset_range(request.headers.get("range"), size)
    except ValueError:
        return Response(status_code=416, headers={"content-range": f"bytes */{size}", "accept-ranges": "bytes"})
    options: dict[str, object] = {}
    response_headers = {"etag": etag, "accept-ranges": "bytes"}
    status_code = 200
    if byte_range is not None:
        start, end = byte_range
        options = {"range": {"offset": start, "length": end - start + 1}}
        response_headers["content-range"] = f"bytes {start}-{end}/{size}"
        status_code = 206
    storage_key = str(row.get("storage_key") or key)
    stored = await env.ASSETS.get(storage_key, options) if options else await env.ASSETS.get(storage_key)
    if not stored:
        return JSONResponse({"error": "asset not found"}, status_code=404)
    checksum = str(row.get("checksum_sha256") or "")
    if checksum:
        response_headers["x-content-sha256"] = checksum
    response_headers["content-length"] = str(byte_range[1] - byte_range[0] + 1 if byte_range else size)
    return StreamingResponse(
        content=_r2_body_chunks(stored),
        media_type=str(row["content_type"]),
        headers=response_headers,
        status_code=status_code,
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
    uid = str(context["uid"])
    now = int(time.time())
    schedule_cleanup = env.APP_DB.prepare(
        "INSERT OR IGNORE INTO cf_asset_cleanup_tasks "
        "(storage_key, uid, logical_key, content_type, reason, not_before, attempts, last_error, "
        "created_at, updated_at) "
        "SELECT storage_key, uid, object_key, content_type, 'deleted', ?, 0, NULL, ?, ? FROM cf_asset_objects "
        "WHERE uid = ? AND object_key = ? AND storage_key IS NOT NULL"
    ).bind(now, now, now, uid, key)
    delete_metadata = env.APP_DB.prepare("DELETE FROM cf_asset_objects WHERE uid = ? AND object_key = ?").bind(uid, key)
    try:
        await env.APP_DB.batch([schedule_cleanup, delete_metadata])
    except Exception:
        return JSONResponse({"error": "asset metadata is unavailable"}, status_code=503)
    try:
        await _drain_asset_cleanup(env, uid, key)
    except Exception:
        pass
    return {"status": "deleted", "key": requested_key.strip("/")}


Default = asgi.entrypoint(app) if asgi else None
