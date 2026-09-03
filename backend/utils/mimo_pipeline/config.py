"""Runtime configuration contract for the optional MiMo provider.

MiMo is an opt-in provider.  There is deliberately no endpoint default here:
an operator must name the OpenAI-compatible authority explicitly.  Keeping the
configuration lookup at the call boundary also means tests and long-running
workers cannot accidentally retain stale environment values from import time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


class MimoConfigurationError(RuntimeError):
    """Raised when the selected MiMo provider is not safely configured."""


@dataclass(frozen=True)
class MimoRuntimeConfig:
    """Resolved credentials and endpoint for one MiMo client."""

    api_key: str
    base_url: str


_BLOCKED_VENDOR_HOST_SUFFIXES = (
    ".xiaomimimo.com",
    ".mimo.mi.com",
)


def _is_blocked_vendor_host(hostname: str) -> bool:
    """Return whether a MiMo URL points at a known vendor authority.

    The endpoint is deliberately operator-selected.  An explicit vendor URL
    is not a deployment-neutral substitute, so reject it before any client is
    constructed.  Private/operator authorities remain valid.
    """

    normalized = hostname.rstrip(".").lower()
    return any(normalized == suffix[1:] or normalized.endswith(suffix) for suffix in _BLOCKED_VENDOR_HOST_SUFFIXES)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def validate_mimo_base_url(value: str, *, env_name: str = "MIMO_API_BASE") -> str:
    """Validate an operator-owned MiMo HTTP authority and normalize its slash.

    Query strings and fragments are forbidden because the client appends a
    fixed API path and credentials must never be mixed into an opaque URL
    suffix.  Userinfo is forbidden so credentials cannot be smuggled through
    the URL.  Private/operator endpoints remain valid; network reachability is
    an operator deployment policy, not a reason to force a public vendor host.
    """

    raw = (value or "").strip()
    if not raw:
        raise MimoConfigurationError(f"{env_name} is required when MiMo is enabled")
    if any(character.isspace() for character in raw):
        raise MimoConfigurationError(f"{env_name} must not contain whitespace")

    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise MimoConfigurationError(f"{env_name} must use http:// or https://")
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise MimoConfigurationError(f"{env_name} has an invalid host or port") from exc
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise MimoConfigurationError(f"{env_name} must not contain userinfo and must include a host")
    if _is_blocked_vendor_host(hostname):
        raise MimoConfigurationError(f"{env_name} must use an operator-owned authority")
    if parsed.query or parsed.fragment:
        raise MimoConfigurationError(f"{env_name} must not contain a query or fragment")
    if port is not None and not (1 <= port <= 65535):
        raise MimoConfigurationError(f"{env_name} has an invalid port")

    return raw.rstrip("/")


def resolve_mimo_base_url(explicit_base_url: str | None = None) -> str:
    """Resolve an explicit endpoint or the selected operator env binding."""

    if explicit_base_url is not None:
        return validate_mimo_base_url(explicit_base_url, env_name="base_url")

    env_name = "MIMO_TOKENPLAN_BASE" if _env_flag("MIMO_USE_TOKENPLAN") else "MIMO_API_BASE"
    return validate_mimo_base_url(os.getenv(env_name, ""), env_name=env_name)


def resolve_mimo_openai_base_url(explicit_base_url: str | None = None) -> str:
    """Return the OpenAI-compatible ``/v1`` root for shared LLM clients."""

    base_url = resolve_mimo_base_url(explicit_base_url)
    return base_url if base_url.endswith("/v1") else f"{base_url}/v1"


def resolve_mimo_api_key(explicit_api_key: str | None = None) -> str:
    """Resolve a non-empty credential without falling back across authorities."""

    value = explicit_api_key if explicit_api_key is not None else os.getenv("MIMO_API_KEY", "")
    if not value or not value.strip():
        raise MimoConfigurationError("MIMO_API_KEY environment variable not set")
    return value.strip()


def resolve_mimo_config(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> MimoRuntimeConfig:
    """Resolve and validate the complete client contract before construction."""

    # Resolve both fields before returning any partially configured client.
    return MimoRuntimeConfig(
        api_key=resolve_mimo_api_key(api_key),
        base_url=resolve_mimo_base_url(base_url),
    )


def mimo_is_configured() -> bool:
    """Return whether the current environment can safely select MiMo."""

    try:
        resolve_mimo_config()
    except MimoConfigurationError:
        return False
    return True


def timeout_seconds(default: float = 120.0) -> float:
    """Read the request timeout at the call boundary with a safe lower bound."""

    raw = os.getenv("MIMO_TIMEOUT_SECONDS", str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise MimoConfigurationError("MIMO_TIMEOUT_SECONDS must be a number") from exc
    if value <= 0:
        raise MimoConfigurationError("MIMO_TIMEOUT_SECONDS must be greater than zero")
    return value
