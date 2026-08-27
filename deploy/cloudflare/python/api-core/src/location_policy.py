import json
from datetime import datetime, timezone


LOCATION_CONTEXT_PURPOSE = "chat_city_context"
LOCATION_CONTEXT_DISCLOSED_PROVIDERS = ("Google Maps", "the configured AI chat provider")
LOCATION_CONTEXT_CONSENT_TTL_SECONDS = 30 * 24 * 60 * 60


def _iso_timestamp(epoch: object) -> str | None:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def location_context_response(row: object | None, *, now: int) -> dict[str, object]:
    """Render the public consent contract, failing closed on malformed D1 rows."""
    if not isinstance(row, dict):
        return {
            "enabled": False,
            "purpose": LOCATION_CONTEXT_PURPOSE,
            "disclosed_providers": list(LOCATION_CONTEXT_DISCLOSED_PROVIDERS),
            "expires_at": None,
        }
    expires_at = row.get("expires_at")
    active = (
        row.get("status") == "granted"
        and row.get("purpose") == LOCATION_CONTEXT_PURPOSE
        and row.get("disclosed_providers_json")
        == json.dumps(LOCATION_CONTEXT_DISCLOSED_PROVIDERS)
        and isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and expires_at > now
        and row.get("revoked_at") is None
    )
    return {
        "enabled": active,
        "purpose": LOCATION_CONTEXT_PURPOSE,
        "disclosed_providers": list(LOCATION_CONTEXT_DISCLOSED_PROVIDERS),
        "expires_at": _iso_timestamp(expires_at) if active else None,
    }
