"""Cloudflare-owned macOS Beta admission and break-glass mutations."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from desktop_beta_evidence import BetaEvidenceError, build_emergency_beta_manifest, build_signed_beta_manifest
from desktop_release_routes import (
    _changes,
    _manifest_canonical_bytes,
    _manifest_from_row,
    _manifest_sha256,
    _pointer_from_row,
    _validate_desktop_manifest,
)

router = APIRouter()

_TAG_RE = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\+(?P<build>[1-9][0-9]*)-macos$")
_BREAKGLASS_TAG_RE = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+(?:\.[0-9]+)?)\+(?P<build>[1-9][0-9]*)-macos$")
_INCIDENT_URL_RE = re.compile(r"^https://github\.com/BasedHardware/omi/(?:issues|discussions)/[1-9][0-9]*(?:[/?#].*)?$")
_REQUEST_ID_RE = re.compile(r"^https://github\.com/BasedHardware/omi/actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*$")


class BetaCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag: str = Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+\+[1-9][0-9]*-macos$")


class BetaAdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promotion_enabled: StrictBool


class BetaBreakglassRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str = Field(pattern="^(rollback|rollout)$")
    current_release_id: str = Field(pattern=r"^v[0-9]+\.[0-9]+(?:\.[0-9]+)?\+[1-9][0-9]*-macos$")
    target_release_id: str = Field(pattern=r"^v[0-9]+\.[0-9]+(?:\.[0-9]+)?\+[1-9][0-9]*-macos$")
    expected_generation: int = Field(ge=0)
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    incident_url: str = Field(
        pattern=r"^https://github\.com/BasedHardware/omi/(?:issues|discussions)/[1-9][0-9]*(?:[/?#].*)?$"
    )
    request_id: str = Field(
        pattern=r"^https://github\.com/BasedHardware/omi/actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*$"
    )
    normal_path_unavailable: str | None = Field(default=None, min_length=1, max_length=1000)


def _tag_parts(tag: object) -> tuple[str, tuple[int, int, int], int]:
    if not isinstance(tag, str):
        raise ValueError("candidate tag must be a canonical macOS tag")
    match = _TAG_RE.fullmatch(tag)
    if match is None:
        raise ValueError("candidate tag must be a canonical macOS tag")
    parts = tuple(int(item) for item in match.group("version").split("."))
    return tag, parts, int(match.group("build"))


def _breakglass_tag_parts(tag: object) -> tuple[str, tuple[int, int, int], int]:
    """Parse retained 2- or 3-component release ids used by rollback requests."""
    if not isinstance(tag, str):
        raise ValueError("release id must be a canonical macOS tag")
    match = _BREAKGLASS_TAG_RE.fullmatch(tag)
    if match is None:
        raise ValueError("release id must be a canonical macOS tag")
    raw_version = match.group("version").split(".")
    version = tuple(int(part) for part in (*raw_version, "0")[:3])
    return tag, version, int(match.group("build"))


def _control(row: object) -> dict[str, object] | None:
    if row is None:
        return None
    if not isinstance(row, dict) or set(row) != {
        "id",
        "schema_version",
        "promotion_enabled",
        "latest_reserved_tag",
        "latest_reserved_build_number",
        "control_generation",
        "latest_reserved_at",
        "admission_updated_at",
    }:
        raise ValueError("beta admission control schema is invalid")
    if row.get("id") != "control" or row.get("schema_version") != 1:
        raise ValueError("beta admission control schema is invalid")
    enabled = row.get("promotion_enabled")
    generation = row.get("control_generation")
    if enabled not in (0, 1) or not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValueError("beta admission control schema is invalid")
    tag = row.get("latest_reserved_tag")
    build = row.get("latest_reserved_build_number")
    reserved_at = row.get("latest_reserved_at")
    if tag is None:
        if build is not None or reserved_at is not None:
            raise ValueError("beta admission control schema is invalid")
    else:
        if not isinstance(tag, str) or not isinstance(build, int) or isinstance(build, bool) or build < 1:
            raise ValueError("beta admission control schema is invalid")
        _, _, parsed_build = _tag_parts(tag)
        if build != parsed_build:
            raise ValueError("beta admission control schema is invalid")
        if not isinstance(reserved_at, int) or reserved_at <= 0:
            raise ValueError("beta admission control schema is invalid")
    updated = row.get("admission_updated_at")
    if not isinstance(updated, int) or updated <= 0:
        raise ValueError("beta admission control schema is invalid")
    return {
        "promotion_enabled": bool(enabled),
        "latest_reserved_tag": tag,
        "latest_reserved_build_number": build,
        "control_generation": generation,
        "latest_reserved_at": reserved_at,
        "admission_updated_at": updated,
    }


async def _read_control(env: object) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(
        "SELECT id, schema_version, promotion_enabled, latest_reserved_tag, latest_reserved_build_number, "
        "control_generation, latest_reserved_at, admission_updated_at FROM cf_desktop_beta_admission "
        "WHERE id = 'control' LIMIT 1"
    ).first()
    return _control(row)


def _bearer_valid(request: Request) -> bool:
    expected = getattr(request.scope["env"], "BETA_PROMOTION_TOKEN", None)
    provided = request.headers.get("authorization")
    return (
        isinstance(expected, str)
        and bool(expected)
        and isinstance(provided, str)
        and provided.startswith("Bearer ")
        and hmac.compare_digest(provided.removeprefix("Bearer "), expected)
    )


def _admin_valid(request: Request) -> bool:
    expected = getattr(request.scope["env"], "ADMIN_KEY", None)
    provided = request.headers.get("secret-key")
    return (
        isinstance(expected, str)
        and bool(expected)
        and isinstance(provided, str)
        and hmac.compare_digest(provided, expected)
    )


async def _reserve(env: object, tag: str) -> dict[str, object]:
    tag, version, build = _tag_parts(tag)
    now = int(time.time())
    current = await _read_control(env)
    if current is None:
        result = (
            await env.APP_DB.prepare(
                "INSERT INTO cf_desktop_beta_admission "
                "(id, schema_version, promotion_enabled, latest_reserved_tag, latest_reserved_build_number, "
                "control_generation, latest_reserved_at, admission_updated_at) VALUES ('control', 1, 0, ?, ?, 1, ?, ?)"
            )
            .bind(tag, build, now, now)
            .run()
        )
        if _changes(result) != 1:
            current = await _read_control(env)
            if current is None or current.get("latest_reserved_tag") != tag:
                raise ValueError("beta admission reservation changed")
            return current
        return {
            "promotion_enabled": False,
            "latest_reserved_tag": tag,
            "latest_reserved_build_number": build,
            "control_generation": 1,
            "latest_reserved_at": now,
            "admission_updated_at": now,
        }
    current_tag = current["latest_reserved_tag"]
    if current_tag == tag:
        return current
    if current_tag is not None:
        _, current_version, current_build = _tag_parts(current_tag)
        if build <= current_build or version < current_version:
            raise ValueError("candidate reservation must roll forward")
    generation = int(current["control_generation"]) + 1
    result = (
        await env.APP_DB.prepare(
            "UPDATE cf_desktop_beta_admission SET latest_reserved_tag = ?, latest_reserved_build_number = ?, "
            "latest_reserved_at = ?, control_generation = ?, admission_updated_at = ? "
            "WHERE id = 'control' AND control_generation = ?"
        )
        .bind(tag, build, now, generation, now, current["control_generation"])
        .run()
    )
    if _changes(result) != 1:
        raise ValueError("beta admission reservation changed")
    return {
        **current,
        "latest_reserved_tag": tag,
        "latest_reserved_build_number": build,
        "control_generation": generation,
        "latest_reserved_at": now,
        "admission_updated_at": now,
    }


async def _set_enabled(env: object, enabled: bool) -> dict[str, object]:
    now = int(time.time())
    current = await _read_control(env)
    if current is None:
        if enabled:
            raise ValueError("beta admission cannot resume without a reservation")
        result = (
            await env.APP_DB.prepare(
                "INSERT INTO cf_desktop_beta_admission "
                "(id, schema_version, promotion_enabled, latest_reserved_tag, latest_reserved_build_number, "
                "control_generation, latest_reserved_at, admission_updated_at) VALUES ('control', 1, 0, NULL, NULL, 1, NULL, ?)"
            )
            .bind(now)
            .run()
        )
        if _changes(result) != 1:
            raise ValueError("beta admission control changed")
        return {
            "promotion_enabled": False,
            "latest_reserved_tag": None,
            "latest_reserved_build_number": None,
            "control_generation": 1,
            "latest_reserved_at": None,
            "admission_updated_at": now,
        }
    if current["promotion_enabled"] is enabled:
        return current
    if enabled and current["latest_reserved_tag"] is None:
        raise ValueError("beta admission cannot resume without a reservation")
    generation = int(current["control_generation"]) + 1
    result = (
        await env.APP_DB.prepare(
            "UPDATE cf_desktop_beta_admission SET promotion_enabled = ?, control_generation = ?, admission_updated_at = ? "
            "WHERE id = 'control' AND control_generation = ?"
        )
        .bind(int(enabled), generation, now, current["control_generation"])
        .run()
    )
    if _changes(result) != 1:
        raise ValueError("beta admission control changed")
    return {**current, "promotion_enabled": enabled, "control_generation": generation, "admission_updated_at": now}


async def _admit(env: object, manifest: dict[str, object], control: dict[str, object]) -> dict[str, object]:
    try:
        manifest = _validate_desktop_manifest(manifest)
    except ValueError as exc:
        raise ValueError("release manifest is invalid") from exc
    tag, version_parts, build = _tag_parts(manifest.get("release_id"))
    version = str(manifest.get("version") or "")
    if tuple(int(part) for part in version.split(".")) != version_parts:
        raise ValueError("release manifest version does not match its release id")
    if not control["promotion_enabled"]:
        raise ValueError("beta admission is disabled")
    if control["latest_reserved_tag"] != tag or control["latest_reserved_build_number"] != build:
        raise ValueError("beta admission reservation does not match candidate")
    existing = (
        await env.APP_DB.prepare(
            "SELECT release_id, manifest_json, manifest_sha256 FROM cf_desktop_release_manifests WHERE release_id = ? LIMIT 1"
        )
        .bind(tag)
        .first()
    )
    if existing is not None:
        parsed = _manifest_from_row(existing, tag)
        if parsed is None or parsed[0] != manifest:
            raise ValueError("release_id already exists with different immutable metadata")
    pointer_row = await env.APP_DB.prepare(
        "SELECT platform, channel, release_id, version, build_number, generation, updated_at "
        "FROM cf_desktop_channel_pointers WHERE platform = 'macos' AND channel = 'beta' LIMIT 1"
    ).first()
    current = _pointer_from_row(pointer_row, platform="macos", channel="beta") if pointer_row else None
    if pointer_row is not None and current is None:
        raise ValueError("beta channel pointer projection is invalid")
    if current is not None and current["release_id"] == tag:
        return {"manifest": manifest, "pointer": current, "idempotent": True}
    if current is not None and build <= int(current["build_number"]):
        raise ValueError("channel pointers are roll-forward only")
    now = int(time.time())
    generation = int(current["generation"]) + 1 if current is not None else 1
    statements: list[Any] = [
        env.APP_DB.prepare(
            "UPDATE cf_desktop_beta_admission SET admission_updated_at = admission_updated_at "
            "WHERE id = 'control' AND control_generation = ? AND promotion_enabled = 1 "
            "AND latest_reserved_tag = ? AND latest_reserved_build_number = ?"
        ).bind(control["control_generation"], tag, build)
    ]
    if existing is None:
        statements.append(
            env.APP_DB.prepare(
                "INSERT INTO cf_desktop_release_manifests "
                "(release_id, manifest_json, manifest_sha256, created_at) VALUES (?, ?, ?, ?)"
            ).bind(tag, _manifest_canonical_bytes(manifest).decode("utf-8"), _manifest_sha256(manifest), now)
        )
    if current is None:
        statements.append(
            env.APP_DB.prepare(
                "INSERT INTO cf_desktop_channel_pointers "
                "(platform, channel, release_id, version, build_number, generation, updated_at) "
                "SELECT 'macos', 'beta', ?, ?, ?, 1, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM cf_desktop_channel_pointers WHERE platform = 'macos' AND channel = 'beta')"
            ).bind(tag, version, build, now)
        )
    else:
        statements.append(
            env.APP_DB.prepare(
                "UPDATE cf_desktop_channel_pointers SET release_id = ?, version = ?, build_number = ?, "
                "generation = ?, updated_at = ? WHERE platform = 'macos' AND channel = 'beta' AND generation = ?"
            ).bind(tag, version, build, generation, now, current["generation"])
        )
    try:
        results = await env.APP_DB.batch(statements)
    except Exception as exc:
        raise ValueError("beta admission transaction conflicted") from exc
    if not results or _changes(results[0]) != 1 or any(_changes(result) != 1 for result in results[1:]):
        raise ValueError("beta admission transaction conflicted")
    pointer = {
        "platform": "macos",
        "channel": "beta",
        "release_id": tag,
        "version": version,
        "build_number": build,
        "generation": generation,
        "updated_at": now,
    }
    return {"manifest": manifest, "pointer": pointer, "idempotent": False}


async def _breakglass(
    env: object, request: BetaBreakglassRequest, manifest: dict[str, object] | None
) -> dict[str, object]:
    current_release = request.current_release_id
    target_release = request.target_release_id
    existing_pointer_row = await env.APP_DB.prepare(
        "SELECT platform, channel, release_id, version, build_number, generation, updated_at "
        "FROM cf_desktop_channel_pointers WHERE platform = 'macos' AND channel = 'beta' LIMIT 1"
    ).first()
    pointer = (
        _pointer_from_row(existing_pointer_row, platform="macos", channel="beta") if existing_pointer_row else None
    )
    if (
        pointer is None
        or pointer["release_id"] != current_release
        or pointer["generation"] != request.expected_generation
    ):
        raise ValueError("current Beta pointer does not match the break-glass precondition")
    control = await _read_control(env)
    if control is None:
        raise ValueError("beta admission control is unavailable")
    if request.operation == "rollout":
        if (
            manifest is None
            or manifest.get("release_id") != target_release
            or manifest.get("qualification_tier") != "emergency"
        ):
            raise ValueError("emergency target manifest is invalid")
        if int(manifest["build_number"]) <= int(pointer["build_number"]):
            raise ValueError("emergency target must have a higher build")
    else:
        target_row = (
            await env.APP_DB.prepare(
                "SELECT release_id, manifest_json, manifest_sha256 FROM cf_desktop_release_manifests WHERE release_id = ? LIMIT 1"
            )
            .bind(target_release)
            .first()
        )
        parsed = _manifest_from_row(target_row, target_release) if target_row else None
        if parsed is None or parsed[0].get("qualification_tier") == "emergency":
            raise ValueError("rollback target manifest does not exist or is emergency")
        manifest = parsed[0]
    target_tag, target_version_parts, target_build = _breakglass_tag_parts(manifest.get("release_id"))
    target_version = str(manifest.get("version") or "")
    raw_target_version = target_version.split(".")
    normalized_target_version = (
        tuple(int(part) for part in (*raw_target_version, "0")[:3]) if raw_target_version else ()
    )
    if normalized_target_version != target_version_parts:
        raise ValueError("target release version does not match its release id")
    if target_tag != target_release:
        raise ValueError("target release identity mismatch")
    audit_id = hashlib.sha256(request.request_id.encode("utf-8")).hexdigest()
    audit = (
        await env.APP_DB.prepare("SELECT audit_id FROM cf_desktop_beta_breakglass_audits WHERE audit_id = ? LIMIT 1")
        .bind(audit_id)
        .first()
    )
    if audit is not None:
        raise ValueError("request was already used")
    now = int(time.time())
    next_control_generation = int(control["control_generation"]) + 1
    next_pointer_generation = int(pointer["generation"]) + 1
    statements: list[Any] = [
        env.APP_DB.prepare(
            "INSERT INTO cf_desktop_beta_breakglass_audits "
            "(audit_id, schema_version, operation, platform, channel, current_release_id, target_release_id, "
            "expected_generation, actor, reason, incident_url, request_id, normal_path_unavailable, "
            "target_manifest_sha256, resulting_generation, created_at) VALUES (?, 1, ?, 'macos', 'beta', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        ).bind(
            audit_id,
            request.operation,
            current_release,
            target_release,
            request.expected_generation,
            request.actor,
            request.reason,
            request.incident_url,
            request.request_id,
            request.normal_path_unavailable,
            _manifest_sha256(manifest),
            next_pointer_generation,
            now,
        ),
        env.APP_DB.prepare(
            "UPDATE cf_desktop_beta_admission SET promotion_enabled = 0, control_generation = ?, admission_updated_at = ? "
            "WHERE id = 'control' AND control_generation = ?"
        ).bind(next_control_generation, now, control["control_generation"]),
        env.APP_DB.prepare(
            "UPDATE cf_desktop_channel_pointers SET release_id = ?, version = ?, build_number = ?, generation = ?, updated_at = ? "
            "WHERE platform = 'macos' AND channel = 'beta' AND release_id = ? AND generation = ?"
        ).bind(
            target_release,
            target_version,
            target_build,
            next_pointer_generation,
            now,
            current_release,
            request.expected_generation,
        ),
    ]
    target_row = (
        await env.APP_DB.prepare("SELECT release_id FROM cf_desktop_release_manifests WHERE release_id = ? LIMIT 1")
        .bind(target_release)
        .first()
    )
    if target_row is None:
        statements.insert(
            1,
            env.APP_DB.prepare(
                "INSERT INTO cf_desktop_release_manifests "
                "(release_id, manifest_json, manifest_sha256, created_at) VALUES (?, ?, ?, ?)"
            ).bind(
                target_release, _manifest_canonical_bytes(manifest).decode("utf-8"), _manifest_sha256(manifest), now
            ),
        )
    try:
        results = await env.APP_DB.batch(statements)
    except Exception as exc:
        raise ValueError("break-glass transaction conflicted") from exc
    if any(_changes(result) != 1 for result in results):
        raise ValueError("break-glass transaction conflicted")
    return {"operation": request.operation, "release_id": target_release, "generation": next_pointer_generation}


@router.post("/v2/desktop/beta/candidates/reserve")
async def reserve_beta_candidate(request: Request, payload: BetaCandidateRequest):
    if not _bearer_valid(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        control = await _reserve(request.scope["env"], payload.tag)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"tag": control["latest_reserved_tag"], "generation": control["control_generation"]}


@router.post("/v2/desktop/beta/promote-candidate")
async def promote_beta_candidate(request: Request, payload: BetaCandidateRequest):
    if not _bearer_valid(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        control = await _read_control(request.scope["env"])
        if control is None:
            raise ValueError("beta admission control is unavailable")
        manifest = await build_signed_beta_manifest(request.scope["env"], payload.tag)
        receipt = await _admit(request.scope["env"], manifest, control)
    except BetaEvidenceError:
        raise HTTPException(status_code=422, detail="Beta candidate rejected") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="Beta candidate promotion conflict") from None
    return {
        "tag": receipt["manifest"]["release_id"],
        "release_id": receipt["manifest"]["release_id"],
        "generation": receipt["pointer"]["generation"],
        "idempotent": receipt["idempotent"],
    }


@router.put("/v2/desktop/beta/admission")
async def set_beta_admission(request: Request, payload: BetaAdmissionRequest):
    if not _admin_valid(request):
        raise HTTPException(status_code=403, detail="You are not authorized to perform this action")
    try:
        control = await _set_enabled(request.scope["env"], payload.promotion_enabled)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"promotion_enabled": control["promotion_enabled"], "generation": control["control_generation"]}


@router.post("/v2/desktop/beta/breakglass")
async def mutate_beta_breakglass(request: Request, payload: BetaBreakglassRequest):
    if not _admin_valid(request):
        raise HTTPException(status_code=403, detail="You are not authorized to perform this action")
    try:
        manifest = None
        if payload.operation == "rollout":
            if payload.normal_path_unavailable is None:
                raise HTTPException(
                    status_code=422, detail="Why normal Beta promotion cannot recover in time is required"
                )
            manifest = await build_emergency_beta_manifest(request.scope["env"], payload.target_release_id)
        return await _breakglass(request.scope["env"], payload, manifest)
    except BetaEvidenceError:
        raise HTTPException(status_code=422, detail="Emergency Beta candidate rejected") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


__all__ = ["router"]
