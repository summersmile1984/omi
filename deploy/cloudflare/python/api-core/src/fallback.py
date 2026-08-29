"""Bounded structured fallback telemetry for the API Core Python Worker."""

from __future__ import annotations

import json


def record_fallback(
    *,
    from_mode: str,
    to_mode: str,
    reason: str,
    outcome: str,
    component: str = "other",
) -> None:
    """Emit the repository-wide fallback event shape without user data."""

    allowed_components = {"auth", "llm", "other"}
    allowed_from = {"auth_worker", "workers_ai", "none"}
    allowed_to = {"metadata_only", "system_default", "none"}
    allowed_reasons = {"dependency_unavailable", "malformed_doc", "other"}
    allowed_outcomes = {"recovered", "degraded", "exhausted"}
    print(
        json.dumps(
            {
                "event": "fallback",
                "component": component if component in allowed_components else "other",
                "from": from_mode if from_mode in allowed_from else "none",
                "to": to_mode if to_mode in allowed_to else "none",
                "reason": reason if reason in allowed_reasons else "other",
                "outcome": outcome if outcome in allowed_outcomes else "degraded",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
