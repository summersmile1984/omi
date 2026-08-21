"""
LangSmith observability configuration and status logging.

This module provides utilities for checking and logging LangSmith tracing status
at application startup, creating per-request tracers for scoped tracing,
and for submitting feedback to LangSmith.
"""

import os
from typing import Optional, List, Any, Dict, Callable, cast
import logging

logger = logging.getLogger(__name__)


_NEUTRAL_DEPLOYMENT_PROFILES = frozenset({'neutral', 'self_hosted', 'self-hosted'})
_LANGSMITH_TRACING_ENV_VARS = (
    'LANGSMITH_TRACING',
    'LANGSMITH_TRACING_V2',
    'LANGCHAIN_TRACING',
    'LANGCHAIN_TRACING_V2',
)


def _is_neutral_deployment() -> bool:
    """Return whether the process is running without Omi-managed egress."""
    return os.environ.get('OMI_DEPLOYMENT_PROFILE', '').strip().lower() in _NEUTRAL_DEPLOYMENT_PROFILES


def _disable_ambient_langsmith_tracing() -> None:
    """Prevent LangChain's implicit LangSmith callback from using ambient credentials.

    LangChain/LangSmith inspect their tracing environment at model invocation time,
    independently of the callbacks returned by this module.  A neutral process may
    inherit managed ``LANGCHAIN_*`` secrets from a shared environment, so simply
    returning an empty callback list is not sufficient: the SDK could still create
    its own ``LangChainTracer`` and post to its default endpoint.  Disable all
    supported tracing aliases before any model call.  This does not remove keys or
    endpoints, preserving managed-profile behavior and leaving operator diagnostics
    intact.
    """
    if not _is_neutral_deployment():
        return

    for env_name in _LANGSMITH_TRACING_ENV_VARS:
        os.environ[env_name] = 'false'

    # The SDK caches environment lookups.  Clear that cache when the optional SDK
    # is already installed so a value read before this guard cannot re-enable tracing.
    try:
        from langsmith import utils as langsmith_utils

        cache_clear = getattr(langsmith_utils.get_env_var, 'cache_clear', None)
        if callable(cache_clear):
            cast(Callable[[], None], cache_clear)()
    except Exception:
        # LangSmith is optional; the public helpers below remain fail-closed without it.
        pass


_disable_ambient_langsmith_tracing()


def is_langsmith_enabled() -> bool:
    """
    Check if LangSmith tracing is enabled via environment variables.

    Checks both new (LANGSMITH_*) and legacy (LANGCHAIN_*) env var formats.

    Returns:
        True if tracing is enabled, False otherwise
    """
    _disable_ambient_langsmith_tracing()
    if _is_neutral_deployment():
        return False

    # Check new-style env vars first
    langsmith_tracing = os.environ.get("LANGSMITH_TRACING", "").lower()
    if langsmith_tracing == "true":
        return True

    # Check legacy env vars
    langchain_tracing = os.environ.get("LANGCHAIN_TRACING_V2", "").lower()
    if langchain_tracing == "true":
        return True

    return False


def get_langsmith_project() -> str:
    """
    Get the configured LangSmith project name.

    Returns:
        Project name or "default" if not set
    """
    return os.environ.get("LANGSMITH_PROJECT") or os.environ.get("LANGCHAIN_PROJECT") or "default"


def get_langsmith_endpoint() -> str:
    """
    Get the configured LangSmith API endpoint.

    Returns:
        Endpoint URL or default LangSmith endpoint
    """
    if _is_neutral_deployment():
        return ''

    return (
        os.environ.get("LANGSMITH_ENDPOINT")
        or os.environ.get("LANGCHAIN_ENDPOINT")
        or "https://api.smith.langchain.com"
    )


def has_langsmith_api_key() -> bool:
    """
    Check if a LangSmith API key is configured.

    Returns:
        True if an API key is set (doesn't validate the key)
    """
    _disable_ambient_langsmith_tracing()
    if _is_neutral_deployment():
        return False

    api_key = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")
    return bool(api_key and len(api_key) > 0 and api_key != "lsv2_pt_REPLACE_WITH_YOUR_KEY")


def log_langsmith_status() -> None:
    """
    Log the current LangSmith tracing configuration status.

    This should be called at application startup to provide visibility
    into whether tracing is properly configured.
    """
    global_enabled = is_langsmith_enabled()
    has_key = has_langsmith_api_key()
    if _is_neutral_deployment():
        logger.info("📊 LangSmith: DISABLED by deployment profile")
        return

    project = get_langsmith_project()
    endpoint = get_langsmith_endpoint()

    if global_enabled and has_key:
        logger.info(f"🔍 LangSmith: GLOBAL tracing ENABLED")
        logger.info(f"   Project: {project}")
        logger.info(f"   Endpoint: {endpoint}")
    elif has_key:
        # Global tracing off but API key present - per-request tracing for chat
        logger.info(f"🔍 LangSmith: Per-request tracing (chat only)")
        logger.info(f"   Project: {project}")
        logger.info(f"   Prompt Hub: enabled")
    else:
        logger.info(f"📊 LangSmith: DISABLED (no API key)")
        logger.info(f"   Set LANGSMITH_API_KEY to enable tracing and prompt fetching")


def get_chat_tracer_callbacks(
    run_id: Optional[str] = None,
    run_name: Optional[str] = None,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    """
    Create LangSmith tracer callbacks for per-request tracing.

    This enables tracing for specific requests (e.g., chat) without enabling
    global tracing. Returns an empty list if API key is not configured.

    Args:
        run_id: Optional explicit run ID for the trace (for feedback attachment)
        run_name: Optional name for the run (e.g., "chat.agentic.stream")
        tags: Optional tags for the run (e.g., ["chat", "agentic"])
        metadata: Optional metadata dict for the run

    Returns:
        List containing LangChainTracer callback if API key is set, else empty list
    """
    if not has_langsmith_api_key():
        return []

    try:
        from langchain_core.tracers import LangChainTracer

        project = get_langsmith_project()

        tracer = LangChainTracer(
            project_name=project,
            tags=tags or [],
        )

        return [tracer]

    except Exception as e:
        logger.error(f"⚠️  Failed to create LangSmith tracer: {e}")
        return []


def submit_langsmith_feedback(
    run_id: str,
    score: float,
    key: str = "user_feedback",
    comment: Optional[str] = None,
) -> bool:
    """
    Submit feedback to LangSmith for a specific run.

    Args:
        run_id: The LangSmith run ID to attach feedback to
        score: Feedback score (typically 0.0 for negative, 1.0 for positive)
        key: Feedback key/category (default: "user_feedback")
        comment: Optional comment/reason for the feedback

    Returns:
        True if feedback was successfully submitted, False otherwise

    Note: Feedback submission only requires an API key, not global tracing.
    The run_id must be from a traced run (e.g., chat requests with per-request tracing).
    """
    if not has_langsmith_api_key():
        logger.warning(f"⚠️  LangSmith feedback skipped: API key not configured")
        return False

    try:
        from langsmith import Client

        client = Client()

        # Submit feedback to LangSmith
        # Note: feedback_source_type defaults to "api" which is valid
        client.create_feedback(  # type: ignore[reportUnknownMemberType]  # langsmith create_feedback partially typed
            run_id=run_id,
            key=key,
            score=score,
            comment=comment,
        )

        logger.info(f"✅ LangSmith feedback submitted: run_id={run_id}, score={score}, key={key}")
        return True

    except Exception as e:
        logger.error(f"❌ LangSmith feedback error: {e}")
        return False
