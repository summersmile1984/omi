"""Atomic D1 feedback events using the upstream envelope; never copy target text."""

from datetime import datetime, timezone
import uuid

from fallback import record_fallback
from feedback_contract import FeedbackEvent, FeedbackReason, FeedbackSurface, FeedbackTargetKind, MAX_COMMENT_LENGTH


def feedback_event_statement(
    env,
    uid: str,
    target_id: str,
    value: int,
    *,
    surface: FeedbackSurface,
    target_kind: FeedbackTargetKind,
    reason: str | None = None,
    comment: str | None = None,
    platform: str | None = None,
    app_version: str | None = None,
    after_mutation: bool = False,
):
    """Append with the caller's projection in one batch, or roll back both.

    A mutation-conditioned event must immediately follow its UPDATE statement.
    This suppresses the event if a concurrently deleted memory was not updated.
    """
    if value not in {-1, 0, 1}:
        raise ValueError('invalid feedback value')
    try:
        normalized_reason = FeedbackReason(reason) if reason else None
    except ValueError:
        normalized_reason = None
        record_fallback(from_mode='none', to_mode='metadata_only', reason='malformed_doc', outcome='degraded')
    event = FeedbackEvent(
        id=str(uuid.uuid4()),
        uid=uid,
        surface=surface,
        target_kind=target_kind,
        target_id=target_id,
        value=value,
        created_at=datetime.now(timezone.utc),
        reason=normalized_reason,
        comment=comment.strip()[:MAX_COMMENT_LENGTH] if comment else None,
        platform=platform,
        app_version=app_version,
    )
    envelope = event.model_dump_json(exclude_none=True)
    # Capture only message coordinates and model provenance, inside the write
    # transaction. Missing legacy targets still record the vote, as upstream does.
    if target_kind == FeedbackTargetKind.chat_message:
        projection = (
            "json_patch(?, COALESCE((SELECT json_object("
            "'chat_session_id', json_extract(message_json, '$.chat_session_id'), "
            "'target_created_at', json_extract(message_json, '$.created_at'), 'app_id', app_id, "
            "'langsmith_run_id', json_extract(message_json, '$.langsmith_run_id'), "
            "'prompt_name', json_extract(message_json, '$.prompt_name'), "
            "'prompt_commit', json_extract(message_json, '$.prompt_commit')) "
            "FROM cf_chat_messages WHERE uid = ? AND id = ?), '{}'))"
        )
        envelope_args = (envelope, uid, target_id)
    else:
        projection = '?'
        envelope_args = (envelope,)
    return env.APP_DB.prepare(
        'INSERT INTO cf_feedback_events (id, uid, surface, target_kind, target_id, value, created_at, event_json) '
        'SELECT ?, ?, ?, ?, ?, ?, ?, ' + projection + (' WHERE changes() > 0' if after_mutation else '')
    ).bind(
        event.id,
        uid,
        surface.value,
        target_kind.value,
        target_id,
        value,
        event.created_at.timestamp(),
        *envelope_args,
    )
