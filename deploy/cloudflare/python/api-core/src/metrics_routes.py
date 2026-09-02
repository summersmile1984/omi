"""Operational metrics served from the Cloudflare D1 authorities.

The legacy ``/metrics`` route exposed a process-local Prometheus registry,
which has no meaning in a stateless Python Worker. The Cloudflare metrics
authority is instead the durable operational state itself: outbox backlogs,
DLQ depth, projection lag, and deletion intents read live from D1 and emitted
in Prometheus exposition format. The bearer-secret boundary is unchanged, and
the route still fails closed — a missing secret, missing APP_DB binding, or
query failure returns 503 rather than a fabricated zero-valued scrape.
"""

from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response


router = APIRouter()

# (metric name, help text, table, status-label column or None)
_STATUS_GAUGES = (
    (
        "omi_notification_outbox_total",
        "Notification outbox rows by status",
        "cf_notification_outbox",
        "status",
    ),
    (
        "omi_integration_webhook_outbox_total",
        "Integration webhook outbox rows by status",
        "cf_integration_webhook_outbox",
        "status",
    ),
    (
        "omi_developer_webhook_outbox_total",
        "Developer webhook outbox rows by status",
        "cf_developer_webhook_outbox",
        "status",
    ),
    (
        "omi_queue_dlq_messages_total",
        "Captured dead-letter queue messages by status",
        "cf_queue_dlq_messages",
        "status",
    ),
    (
        "omi_sync_jobs_total",
        "Sync jobs by status",
        "cf_sync_jobs",
        "status",
    ),
)

_COUNT_GAUGES = (
    (
        "omi_account_deletion_intents_total",
        "Open account deletion intents",
        "SELECT COUNT(*) AS value FROM cf_account_deletion_intents",
    ),
    (
        "omi_app_webhooks_disabled_total",
        "Apps whose webhook delivery is auto-disabled",
        "SELECT COUNT(*) AS value FROM cf_app_webhook_health WHERE disabled = 1",
    ),
    (
        "omi_vector_projection_outbox_depth",
        "Pending vector projection outbox rows",
        "SELECT COUNT(*) AS value FROM cf_vector_projection_outbox",
    ),
)


def _unavailable() -> JSONResponse:
    return JSONResponse(
        {"error": "metrics_unavailable"},
        status_code=503,
        headers={"cache-control": "no-store"},
    )


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"detail": "Unauthorized"},
        status_code=401,
        headers={"cache-control": "no-store"},
    )


def _label_value(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


async def _exposition(db: object, now: int) -> str:
    lines: list[str] = []
    for name, help_text, table, label in _STATUS_GAUGES:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        rows = (
            await db.prepare(f"SELECT {label} AS label, COUNT(*) AS value FROM {table} GROUP BY {label}")
            .bind()
            .all()
        )
        for row in (rows or {}).get("results", []):
            if isinstance(row, dict):
                lines.append(f'{name}{{status="{_label_value(row.get("label"))}"}} {int(row.get("value") or 0)}')
    for name, help_text, query in _COUNT_GAUGES:
        row = await db.prepare(query).bind().first()
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {int(row.get('value') or 0) if isinstance(row, dict) else 0}")
    oldest = (
        await db.prepare("SELECT MIN(created_at) AS value FROM cf_vector_projection_outbox").bind().first()
    )
    lines.append("# HELP omi_vector_projection_outbox_oldest_age_seconds Age of the oldest pending vector projection")
    lines.append("# TYPE omi_vector_projection_outbox_oldest_age_seconds gauge")
    oldest_created = oldest.get("value") if isinstance(oldest, dict) else None
    age = max(0, now - int(oldest_created)) if oldest_created is not None else 0
    lines.append(f"omi_vector_projection_outbox_oldest_age_seconds {age}")
    lines.append("# HELP omi_metrics_scrape_timestamp_seconds Unix time this scrape was computed")
    lines.append("# TYPE omi_metrics_scrape_timestamp_seconds gauge")
    lines.append(f"omi_metrics_scrape_timestamp_seconds {now}")
    return "\n".join(lines) + "\n"


@router.get("/metrics")
async def get_metrics(request: Request):
    """Serve the D1-backed operational scrape behind the bearer boundary."""
    env = request.scope["env"]
    expected = getattr(env, "METRICS_SECRET", None)
    if not isinstance(expected, str) or not expected:
        return _unavailable()

    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return _unauthorized()
    token = authorization.removeprefix("Bearer ")
    if not token or not hmac.compare_digest(token, expected):
        return _unauthorized()

    db = getattr(env, "APP_DB", None)
    if db is None:
        return _unavailable()
    try:
        body = await _exposition(db, int(time.time()))
    except Exception:
        return _unavailable()
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"cache-control": "no-store"},
    )
