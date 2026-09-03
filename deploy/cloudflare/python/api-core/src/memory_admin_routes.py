"""D1-backed memory admin audit routes for the Cloudflare staging profile.

The non-active-route report is a read-only projection boundary.  It requires
the same server-owned admin key as the legacy route, but it never reads
Firestore and never treats a missing D1 projection as a successful migration.
Short-term lifecycle execution remains a separate Jobs/Queue migration until
its Firestore transition writer has a D1 authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

MAX_UID_LENGTH = 256
MAX_RUN_ID_LENGTH = 256
MAX_SOURCE_ID_LENGTH = 256
MAX_EXPECTED_SOURCE_IDS = 500
MAX_REPORT_ROWS = 2_000
ADMIN_CUTOVER_STATE = "new"
ADMIN_CUTOVER_PHASE = "completed"
NON_ACTIVE_ROUTES = ("review", "archive", "context_only", "reject", "hidden", "skip")
PRESERVED_ROUTES = frozenset({"review", "archive", "context_only", "hidden"})
LOSS_ROUTES = frozenset({"reject", "skip"})


def _admin_key_valid(request: Request) -> bool:
    expected = getattr(request.scope.get("env"), "ADMIN_KEY", None)
    provided = request.headers.get("secret-key")
    return (
        isinstance(expected, str)
        and bool(expected)
        and isinstance(provided, str)
        and hmac.compare_digest(provided, expected)
    )


def _strict_d1_bool(value: object, field: str) -> bool:
    if type(value) is int and value in (0, 1):
        return bool(value)
    if type(value) is bool:
        return value
    raise ValueError(f"invalid {field}")


def _valid_uid(uid: str) -> bool:
    return bool(uid) and len(uid) <= MAX_UID_LENGTH and "/" not in uid and "\x00" not in uid


def _admin_error(reason: str, status_code: int = 503) -> JSONResponse:
    return JSONResponse({"error": "memory admin unavailable", "reason": reason}, status_code=status_code)


async def _read_account_authority(env: object, uid: str) -> dict[str, int] | JSONResponse:
    """Require a completed destination-bound D1 account before reading a report."""

    try:
        now = int(datetime.now(timezone.utc).timestamp())
        fence = (
            await env.APP_DB.prepare(
                "SELECT lifecycle FROM ("
                "SELECT 'deleting' AS lifecycle, 0 AS priority FROM cf_account_deletion_intents WHERE uid = ? "
                "UNION ALL SELECT 'deleted' AS lifecycle, 1 AS priority "
                "FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?"
                ") ORDER BY priority LIMIT 1"
            )
            .bind(uid, uid, now)
            .first()
        )
        if fence is not None:
            if not isinstance(fence, dict) or fence.get("lifecycle") not in {"deleting", "deleted"}:
                return _admin_error("malformed_account_deletion_fence")
            return _admin_error("account_deletion_in_progress", 409)

        cutover = (
            await env.APP_DB.prepare(
                "SELECT uid, schema_version, state, account_generation, checkpoint_phase, "
                "destination_backend_bound FROM cf_account_cutover WHERE uid = ?"
            )
            .bind(uid)
            .first()
        )
        if not isinstance(cutover, dict) or cutover.get("uid") != uid:
            return _admin_error("missing_completed_cutover")
        generation = cutover.get("account_generation")
        bound = _strict_d1_bool(cutover.get("destination_backend_bound"), "destination_backend_bound")
        if (
            cutover.get("schema_version") != 1
            or cutover.get("state") != ADMIN_CUTOVER_STATE
            or cutover.get("checkpoint_phase") != ADMIN_CUTOVER_PHASE
            or not bound
            or type(generation) is not int
            or generation < 0
        ):
            return _admin_error("malformed_completed_cutover")
        return {"account_generation": generation}
    except ValueError:
        return _admin_error("malformed_completed_cutover")
    except Exception:
        return _admin_error("account_authority_unavailable")


def _parse_expected_source_ids(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    values = [value.strip() for value in raw.split(",") if value.strip()]
    if len(values) > MAX_EXPECTED_SOURCE_IDS or any(len(value) > MAX_SOURCE_ID_LENGTH for value in values):
        raise ValueError("expected_source_ids exceeds size limit")
    return sorted(set(values)) or None


def _json_array(value: object, field: str) -> list[object]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 65_536:
        raise ValueError(f"invalid {field}")
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError(f"invalid {field}")
    return parsed


def _json_object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 65_536:
        raise ValueError(f"invalid {field}")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"invalid {field}")
    return parsed


def _required_text(row: dict[str, object], field: str, max_length: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"invalid {field}")
    return value


def _created_at(value: object) -> str:
    if type(value) is not int or value < 0:
        raise ValueError("invalid created_at")
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _coerce_report_row(row: object, uid: str, generation: int) -> dict[str, object]:
    if not isinstance(row, dict) or row.get("uid") != uid or row.get("account_generation") != generation:
        raise ValueError("non-active route uid or generation mismatch")
    route = _required_text(row, "route", 32)
    if route not in NON_ACTIVE_ROUTES:
        raise ValueError("invalid non-active route")
    source_ids = _json_array(row.get("source_ids_json"), "source_ids_json")
    normalized_source_ids = []
    for source_id in source_ids:
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > MAX_SOURCE_ID_LENGTH:
            raise ValueError("invalid source id")
        normalized_source_ids.append(source_id.strip())
    if not normalized_source_ids:
        raise ValueError("source_ids must not be empty")
    audit_metadata = _json_object(row.get("audit_metadata_json"), "audit_metadata_json")
    default_visible = _strict_d1_bool(row.get("default_long_term_visible"), "default_long_term_visible")
    return {
        "uid": uid,
        "outcome_id": _required_text(row, "outcome_id", 256),
        "route": route,
        # Preserve duplicate source references.  The legacy audit treats a
        # duplicate within one outcome as a red terminal-outcome collision;
        # normalizing with set() here would silently erase that evidence.
        "source_ids": sorted(normalized_source_ids),
        "run_id": _required_text(row, "run_id", MAX_RUN_ID_LENGTH),
        "patch_id": row.get("patch_id") if isinstance(row.get("patch_id"), str) else None,
        "created_at": _created_at(row.get("created_at")),
        "audit_metadata": audit_metadata,
        "default_long_term_visible": default_visible,
    }


def _report(uid: str, rows: list[dict[str, object]], expected_source_ids: list[str] | None) -> dict[str, object]:
    counts_by_route = {route: 0 for route in NON_ACTIVE_ROUTES}
    evidence: list[dict[str, object]] = []
    red_reasons: list[str] = []
    source_counts: dict[str, int] = {}
    observed_sources: set[str] = set()
    for row in rows:
        route = str(row["route"])
        counts_by_route[route] += 1
        if row["default_long_term_visible"]:
            red_reasons.append(f"non-active route {row['outcome_id']} is default Long-term visible")
        for source_id in row["source_ids"]:
            observed_sources.add(str(source_id))
            source_counts[str(source_id)] = source_counts.get(str(source_id), 0) + 1
        metadata = row["audit_metadata"]
        remediation_state = metadata.get("remediation_state")
        if not isinstance(remediation_state, str) or not remediation_state.strip():
            remediation_state = "accounted_terminal_outcome"
        preserved = metadata.get("preserved")
        if type(preserved) is not bool:
            preserved = route in PRESERVED_ROUTES
        observable_loss = metadata.get("observable_loss")
        if type(observable_loss) is not bool:
            observable_loss = route in LOSS_ROUTES
        evidence.append(
            {
                "uid": uid,
                "outcome_id": row["outcome_id"],
                "route": route,
                "source_ids": row["source_ids"],
                "terminal_outcome": f"non_active_route:{route}",
                "run_id": row["run_id"],
                "patch_id": row["patch_id"],
                "created_at": row["created_at"],
                "remediation_state": remediation_state,
                "preserved": preserved,
                "observable_loss": observable_loss,
                "accounted": True,
                "default_long_term_visible": row["default_long_term_visible"],
            }
        )
    for source_id in sorted(source_counts):
        if source_counts[source_id] > 1:
            red_reasons.append(f"duplicate terminal outcomes for source {source_id}")
    missing_source_ids = []
    if expected_source_ids is not None:
        missing_source_ids = [source_id for source_id in expected_source_ids if source_id not in observed_sources]
        red_reasons.extend(f"missing terminal outcome for source {source_id}" for source_id in missing_source_ids)
    evidence.sort(key=lambda item: (str(item["route"]), str(item["outcome_id"])))
    return {
        "uid": uid,
        "status": "red" if red_reasons else "green",
        "total_accounted_outcomes": len(evidence),
        "counts_by_route": counts_by_route,
        "evidence": evidence,
        "missing_source_ids": missing_source_ids,
        "red_reasons": red_reasons,
    }


@router.get("/memory/admin/users/{uid}/non-active-route-report")
async def get_non_active_route_report(request: Request, uid: str):
    """Read the uid-scoped D1 non-active route report with an admin key."""

    if not _admin_key_valid(request):
        return JSONResponse({"detail": "You are not authorized to perform this action"}, status_code=403)
    if not _valid_uid(uid):
        return _admin_error("invalid_uid", 400)
    run_id = request.query_params.get("run_id")
    if run_id is not None and (not run_id.strip() or len(run_id) > MAX_RUN_ID_LENGTH):
        return _admin_error("invalid_run_id", 400)
    try:
        expected_source_ids = _parse_expected_source_ids(request.query_params.get("expected_source_ids"))
    except ValueError:
        return _admin_error("invalid_expected_source_ids", 400)

    authority = await _read_account_authority(request.scope["env"], uid)
    if isinstance(authority, JSONResponse):
        return authority
    generation = authority["account_generation"]
    where = "uid = ? AND account_generation = ?"
    args: list[object] = [uid, generation]
    if run_id is not None:
        where += " AND run_id = ?"
        args.append(run_id)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT uid, outcome_id, route, source_ids_json, run_id, patch_id, created_at, "
                "audit_metadata_json, default_long_term_visible, account_generation "
                "FROM cf_memory_non_active_routes WHERE " + where + " ORDER BY route ASC, outcome_id ASC LIMIT ?"
            )
            .bind(*args, MAX_REPORT_ROWS + 1)
            .all()
        )
        raw_rows = result.get("results", []) if isinstance(result, dict) else []
        if not isinstance(raw_rows, list):
            return _admin_error("invalid_report_projection")
        if len(raw_rows) > MAX_REPORT_ROWS:
            return _admin_error("report_projection_exceeds_limit")
        rows = [_coerce_report_row(row, uid, generation) for row in raw_rows]
        return _report(uid, rows, expected_source_ids)
    except ValueError:
        return _admin_error("invalid_report_projection")
    except Exception:
        return _admin_error("report_unavailable")


__all__ = ["get_non_active_route_report", "router"]
