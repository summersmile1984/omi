"""Bounded structured fallback telemetry for the Python Worker."""

from __future__ import annotations

import json


def record_fallback(*, from_mode: str, to_mode: str, reason: str, outcome: str) -> None:
    allowed_from = {"d1", "restrict", "none"}
    allowed_to = {"throttle", "none"}
    allowed_reasons = {"dependency_unavailable", "malformed_doc", "other"}
    allowed_outcomes = {"recovered", "degraded", "exhausted"}
    print(
        json.dumps(
            {
                "event": "fallback",
                "component": "other",
                "from": from_mode if from_mode in allowed_from else "none",
                "to": to_mode if to_mode in allowed_to else "none",
                "reason": reason if reason in allowed_reasons else "other",
                "outcome": outcome if outcome in allowed_outcomes else "degraded",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
