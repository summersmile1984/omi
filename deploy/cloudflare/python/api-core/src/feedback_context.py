"""Metadata-only report windows and bounded on-demand text reads from App D1."""

from datetime import datetime, timezone
import json

from feedback_contract import (
    FeedbackContextHydrated,
    FeedbackContextPointer,
    FeedbackContextTurn,
    FeedbackContextTurnText,
    FeedbackEvent,
    FeedbackTargetKind,
)
from feedback_policy import FOLLOW_UP_WINDOW_SECONDS, MAX_PRECEDING_TURNS, MAX_FOLLOW_UP_TURNS, MAX_HYDRATED_TEXT_CHARS
from feedback_message_time import TIMESTAMP_US

# Apply at the same SQL read as the data, including while physical erasure is queued.
LIVE_EVENT = (
    'NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = e.uid) '
    'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones d WHERE d.uid = e.uid)'
)


async def get_event(env, event_id: str) -> FeedbackEvent | None:
    row = (
        await env.APP_DB.prepare('SELECT e.event_json FROM cf_feedback_events e WHERE e.id = ? AND ' + LIVE_EVENT)
        .bind(event_id)
        .first()
    )
    return FeedbackEvent.model_validate_json(row['event_json']) if row else None


async def resolve_context(env, event: FeedbackEvent) -> FeedbackContextPointer:
    pointer = FeedbackContextPointer(
        event_id=event.id,
        uid=event.uid,
        target_kind=event.target_kind,
        target_id=event.target_id,
    )
    if event.target_kind != FeedbackTargetKind.chat_message:
        return pointer
    # One bounded query per event; none of the SELECT outputs contains message text.
    fields = f"m.id, {TIMESTAMP_US} AS timestamp_us, json_extract(m.message_json, '$.sender') AS sender, json_extract(m.message_json, '$.chat_session_id') AS session_id"
    rows = (
        await env.APP_DB.prepare(
            'WITH rated AS (SELECT ' + fields + ' FROM cf_chat_messages m WHERE m.uid = ? AND m.id = ?), '
            'preceding AS (SELECT ' + fields + ' FROM cf_chat_messages m, rated r '
            "WHERE m.uid = ? AND json_extract(m.message_json, '$.chat_session_id') = COALESCE(?, r.session_id) "
            f'AND {TIMESTAMP_US} < r.timestamp_us ORDER BY timestamp_us DESC, m.id DESC LIMIT ?), '
            'following AS (SELECT ' + fields + ' FROM cf_chat_messages m, rated r '
            f'WHERE m.uid = ? AND {TIMESTAMP_US} > r.timestamp_us AND {TIMESTAMP_US} <= r.timestamp_us + ? '
            'ORDER BY timestamp_us, m.id LIMIT ?) '
            "SELECT *, 'rated' AS position FROM rated UNION ALL SELECT *, 'before' FROM preceding "
            "UNION ALL SELECT *, 'after' FROM following"
        )
        .bind(
            event.uid,
            event.target_id,
            event.uid,
            event.chat_session_id,
            MAX_PRECEDING_TURNS + 1,
            event.uid,
            FOLLOW_UP_WINDOW_SECONDS * 1_000_000,
            MAX_FOLLOW_UP_TURNS + 1,
        )
        .all()
    )
    values = rows.get('results', [])
    rated = next((row for row in values if row['position'] == 'rated'), None)
    if not rated:
        pointer.resolution_error = 'rated_message_not_found'
        return pointer
    if rated['timestamp_us'] is None:
        pointer.resolution_error = 'rated_message_missing_created_at'
        return pointer
    before = sorted(
        (row for row in values if row['position'] == 'before'), key=lambda row: (row['timestamp_us'], row['id'])
    )
    after = sorted(
        (row for row in values if row['position'] == 'after'), key=lambda row: (row['timestamp_us'], row['id'])
    )
    pointer.truncated_before = len(before) > MAX_PRECEDING_TURNS
    pointer.truncated_after = len(after) > MAX_FOLLOW_UP_TURNS
    pointer.follow_up_window_seconds = FOLLOW_UP_WINDOW_SECONDS
    pointer.follow_up_count = min(len(after), MAX_FOLLOW_UP_TURNS)
    if not (event.chat_session_id or rated['session_id']):
        pointer.resolution_error = 'preceding_turns_session_unknown'
    pointer.turns = [
        FeedbackContextTurn(
            message_id=row['id'],
            sender=row['sender'] or '',
            created_at=datetime.fromtimestamp(row['timestamp_us'] / 1_000_000, timezone.utc),
            chat_session_id=row['session_id'],
            position=row['position'],
            seconds_from_rated=int((row['timestamp_us'] - rated['timestamp_us']) / 1_000_000),
        )
        for row in [*before[-MAX_PRECEDING_TURNS:], rated, *after[:MAX_FOLLOW_UP_TURNS]]
    ]
    return pointer


async def hydrate_context(env, event: FeedbackEvent, pointer: FeedbackContextPointer) -> FeedbackContextHydrated:
    if (pointer.uid, pointer.event_id, pointer.target_kind, pointer.target_id) != (
        event.uid,
        event.id,
        event.target_kind,
        event.target_id,
    ):
        raise ValueError('feedback pointer owner mismatch')
    result = FeedbackContextHydrated(event_id=event.id, target_kind=event.target_kind, target_id=event.target_id)
    if event.target_kind != FeedbackTargetKind.chat_message:
        return result
    if len(pointer.turns) > MAX_PRECEDING_TURNS + 1 + MAX_FOLLOW_UP_TURNS:
        raise ValueError('feedback pointer exceeds window')
    ids = [turn.message_id for turn in pointer.turns]
    # The erasure fence and event ownership are rechecked within this text query.
    rows = (
        await env.APP_DB.prepare(
            "SELECT m.id, json_extract(m.message_json, '$.text') AS text, "
            "json_extract(m.message_json, '$.data_protection_level') AS protection "
            'FROM cf_chat_messages m JOIN cf_feedback_events e ON e.uid = m.uid '
            'WHERE e.id = ? AND e.uid = ? AND m.id IN (SELECT value FROM json_each(?)) AND ' + LIVE_EVENT
        )
        .bind(event.id, event.uid, json.dumps(ids))
        .all()
    )
    messages = {row['id']: row for row in rows.get('results', [])}
    for turn in pointer.turns:
        message = messages.get(turn.message_id)
        # Imported enhanced ciphertext cannot be decoded by this native D1 reader.
        if not message or message['protection'] == 'enhanced' or not isinstance(message['text'], str):
            result.unavailable.append(turn.message_id)
            continue
        text = message['text']
        if len(text) > MAX_HYDRATED_TEXT_CHARS:
            text = text[:MAX_HYDRATED_TEXT_CHARS] + '… [truncated]'
        result.turns.append(
            FeedbackContextTurnText(
                message_id=turn.message_id,
                sender=turn.sender,
                created_at=turn.created_at,
                position=turn.position,
                seconds_from_rated=turn.seconds_from_rated,
                text=text,
            )
        )
    return result
