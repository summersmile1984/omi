"""D1-backed account cutover control authority for the Cloudflare profile.

Missing rows remain legacy-compatible everywhere except the explicitly named
isolated staging profile. A Better Auth principal in that profile cannot own
historical Omi data, so its first control read atomically establishes a bound
``new`` account before product traffic is admitted. Existing-account operator
transitions and data imports remain separate controlled workflows.
"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

STATES = frozenset({"legacy", "migrating", "new", "rolled_back_stranded"})
OFFLINE_INSTRUCTIONS = frozenset({"none", "drain", "quarantine"})
CHECKPOINT_PHASES = frozenset(
    {
        "not_started",
        "inventory",
        "offline_queue_fenced",
        "exporting",
        "importing",
        "verifying",
        "cutover_ready",
        "completed",
        "failed",
        "paused",
    }
)
PLATFORMS = ("android", "ios", "linux", "macos", "web", "windows")
MAX_TOKEN_LENGTH = 128
STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ISOLATED_STAGING_PROFILE = "isolated-staging"
ISOLATED_STAGING_MANIFEST = "isolated-staging-v1"


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _bool(value: object) -> bool:
    return bool(value) and value not in ("0", "false", "False", "no")


def _nonnegative_int(row: dict[str, object], key: str) -> int:
    value = row.get(key, 0)
    if isinstance(value, bool):
        raise ValueError(f"invalid {key}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {key}") from error
    if parsed < 0:
        raise ValueError(f"invalid {key}")
    return parsed


def _parse_client_build(raw: object) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if "+" in text:
        text = text.rsplit("+", 1)[-1]
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value >= 0 else None


def _header(request: Request, name: str) -> str | None:
    value = request.headers.get(name)
    return value if isinstance(value, str) else None


def _control(
    row: dict[str, object] | None,
    uid: str,
    *,
    platform: str | None,
    build: str | None,
    version: str | None,
) -> dict[str, object]:
    if row is None:
        record = {
            "state": "legacy",
            "account_generation": 0,
            "ui_generation": 0,
            "api_generation": 0,
            "stranded_new_data": False,
            "offline_queue_instruction": "none",
            "checkpoint_phase": "not_started",
            "checkpoint_token": None,
            "manifest_id": None,
            "destination_backend_bound": False,
        }
    else:
        if row.get("uid") != uid or int(row.get("schema_version", 0)) != 1:
            raise ValueError("invalid account cutover binding")
        state = row.get("state")
        offline = row.get("offline_queue_instruction")
        phase = row.get("checkpoint_phase")
        if state not in STATES or offline not in OFFLINE_INSTRUCTIONS or phase not in CHECKPOINT_PHASES:
            raise ValueError("invalid account cutover enum")
        token = row.get("checkpoint_token")
        manifest_id = row.get("manifest_id")
        if token is not None and (not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH):
            raise ValueError("invalid checkpoint token")
        if manifest_id is not None and (not isinstance(manifest_id, str) or not STABLE_ID.fullmatch(manifest_id)):
            raise ValueError("invalid manifest id")
        record = {
            "state": state,
            "account_generation": _nonnegative_int(row, "account_generation"),
            "ui_generation": _nonnegative_int(row, "ui_generation"),
            "api_generation": _nonnegative_int(row, "api_generation"),
            "stranded_new_data": _bool(row.get("stranded_new_data")),
            "offline_queue_instruction": offline,
            "checkpoint_phase": phase,
            "checkpoint_token": token,
            "manifest_id": manifest_id,
            "destination_backend_bound": _bool(row.get("destination_backend_bound")),
        }

    state = str(record["state"])
    destination_bound = bool(record["destination_backend_bound"])
    checkpoint_phase = str(record["checkpoint_phase"])
    if state == "new" and (not destination_bound or checkpoint_phase != "completed"):
        raise ValueError("new account is not bound to a completed destination")
    normalized_offline = str(record["offline_queue_instruction"])
    if state in {"migrating", "rolled_back_stranded"}:
        normalized_offline = "quarantine"
    # Build floors remain zero until an operator-approved bridge release. Parse
    # the headers now so versioned client formats are accepted from day one.
    _parse_client_build(build if build is not None else version)
    del platform
    action = "migration_maintenance" if state == "migrating" else "none"
    traffic_allowed = state in {"legacy", "rolled_back_stranded"} or (state == "new" and destination_bound)
    ui_generation = 0 if state == "legacy" else int(record["ui_generation"])
    api_generation = 0 if state == "legacy" else int(record["api_generation"])
    return {
        "schema_version": 1,
        "state": state,
        "account_generation": int(record["account_generation"]),
        "ui_generation": ui_generation,
        "api_generation": api_generation,
        "client_action": action,
        "offline_queue_instruction": normalized_offline,
        "stranded_new_data": bool(record["stranded_new_data"]),
        "legacy_writes_allowed": state in {"legacy", "rolled_back_stranded"},
        "product_traffic_allowed": traffic_allowed,
        "auth_bootstrap_reachable": True,
        "minimum_supported_builds": [{"platform": item, "minimum_supported_build": 0} for item in PLATFORMS],
        "migration": {
            "manifest_id": record["manifest_id"],
            "schema_version": 1,
            "checkpoint_phase": record["checkpoint_phase"],
            "checkpoint_token": record["checkpoint_token"],
            "destination_backend_bound": destination_bound,
            "stranded_new_data": bool(record["stranded_new_data"]),
        },
    }


async def _initialize_isolated_staging_account(env: object, context: dict[str, object], uid: str) -> bool:
    if getattr(env, "ACCOUNT_CUTOVER_PROFILE", None) != ISOLATED_STAGING_PROFILE:
        return False
    if context.get("authority") != "better-auth":
        return False
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_account_cutover "
        "(uid, schema_version, state, account_generation, ui_generation, api_generation, stranded_new_data, "
        "offline_queue_instruction, checkpoint_phase, checkpoint_token, manifest_id, destination_backend_bound, "
        "updated_at) VALUES (?, 1, 'new', 1, 1, 1, 0, 'none', 'completed', NULL, ?, 1, ?) "
        "ON CONFLICT(uid) DO NOTHING"
    ).bind(uid, ISOLATED_STAGING_MANIFEST, now).run()
    return True


async def _read_cutover_row(env: object, uid: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT uid, schema_version, state, account_generation, ui_generation, api_generation, "
            "stranded_new_data, offline_queue_instruction, checkpoint_phase, checkpoint_token, manifest_id, "
            "destination_backend_bound FROM cf_account_cutover WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
    if row is not None and not isinstance(row, dict):
        raise ValueError("invalid account cutover row")
    return row


@router.get("/v1/account/cutover/control")
async def get_account_cutover_control(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    try:
        env = request.scope["env"]
        row = await _read_cutover_row(env, uid)
        if row is None and await _initialize_isolated_staging_account(env, context, uid):
            row = await _read_cutover_row(env, uid)
        return _control(
            row,
            uid,
            platform=_header(request, "x-app-platform"),
            build=_header(request, "x-app-build"),
            version=_header(request, "x-app-version"),
        )
    except ValueError:
        return JSONResponse(
            {"error": "account cutover state unavailable", "retryable": True},
            status_code=503,
        )
    except Exception:
        return JSONResponse({"error": "account cutover unavailable"}, status_code=503)


__all__ = ["router"]
