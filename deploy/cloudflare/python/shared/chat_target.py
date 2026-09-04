"""Single D1 target/commit authority shared by the two Python Workers.

The fixed Python builder projects this source into each isolated Worker stage. Explicit
IDs follow backend/utils/chat_session_target.py: owner lookup precedes app scope.
"""

from dataclasses import dataclass
import json
import time
import uuid

APP_SCOPE = "NULLIF(NULLIF(app_id, ''), 'null')"


@dataclass(frozen=True)
class ChatTarget:
    uid: str
    session_id: str | None
    app_id: str | None
    epoch: str = ''
    explicit: bool = False
    new: bool = False


async def resolve_chat_target(env, uid, app_id, requested_id, *, create=False):
    explicit = bool(requested_id)
    if explicit:
        row = (
            await env.APP_DB.prepare('SELECT id, app_id, clear_epoch FROM cf_chat_sessions WHERE uid = ? AND id = ?')
            .bind(uid, requested_id)
            .first()
        )
        if not isinstance(row, dict):
            raise LookupError('Chat session not found')
        app_id = row.get('app_id')
        if app_id in ('', 'null'):
            app_id = None
    else:
        row = (
            await env.APP_DB.prepare(
                f'SELECT id, app_id, clear_epoch FROM cf_chat_sessions WHERE uid = ? AND {APP_SCOPE} IS ? '
                'ORDER BY updated_at DESC, id DESC LIMIT 1'
            )
            .bind(uid, app_id)
            .first()
        )
    if isinstance(row, dict):
        return ChatTarget(uid, str(row['id']), app_id, str(row['clear_epoch']), explicit)
    if create:
        return ChatTarget(uid, str(uuid.uuid4()), app_id, str(uuid.uuid4()), new=True)
    return ChatTarget(uid, None, app_id)


async def admit_chat_target(env, target, reservation_key=None):
    """Make the first default owner visible before any provider call.

    App authorization and quota reservation belong before this transition. Two
    first requests converge in one D1 batch; clear can then fence either one by
    its captured epoch. A failed model may leave an empty, publicly clearable row.
    """
    if not target.new:
        return target
    now = int(time.time())
    available = f'SELECT id FROM cf_chat_sessions WHERE uid = ? AND {APP_SCOPE} IS ?'
    quota_guard = ''
    quota_args = ()
    if reservation_key is not None:
        quota_guard = (
            ' AND EXISTS (SELECT 1 FROM cf_chat_quota_events WHERE uid = ? AND idempotency_key = ? '
            'AND chat_session_id = ? AND settled_at IS NULL)'
        )
        quota_args = (target.uid, reservation_key, target.session_id)
    statements = [
        env.APP_DB.prepare(
            'INSERT INTO cf_chat_sessions (uid, id, title, created_at, updated_at, app_id, clear_epoch) '
            "SELECT ?, ?, 'New Chat', ?, ?, ?, ? WHERE NOT EXISTS (" + available + ')' + quota_guard
        ).bind(
            target.uid,
            target.session_id,
            now,
            now,
            target.app_id,
            target.epoch,
            target.uid,
            target.app_id,
            *quota_args,
        )
    ]
    if reservation_key is not None:
        statements.append(
            env.APP_DB.prepare(
                'UPDATE cf_chat_quota_events SET chat_session_id = ('
                + available
                + ' ORDER BY updated_at DESC, id DESC LIMIT 1) '
                'WHERE uid = ? AND idempotency_key = ? AND chat_session_id = ? AND settled_at IS NULL'
            ).bind(target.uid, target.app_id, *quota_args)
        )
    statements.append(
        env.APP_DB.prepare(
            'SELECT id, clear_epoch FROM cf_chat_sessions WHERE uid = ? AND '
            + APP_SCOPE
            + ' IS ? '
            + (
                'AND EXISTS (SELECT 1 FROM cf_chat_quota_events q WHERE q.uid = cf_chat_sessions.uid '
                'AND q.idempotency_key = ? AND q.chat_session_id = cf_chat_sessions.id AND q.settled_at IS NULL) '
                if reservation_key is not None
                else ''
            )
            + 'ORDER BY updated_at DESC, id DESC LIMIT 1'
        ).bind(target.uid, target.app_id, *((reservation_key,) if reservation_key is not None else ()))
    )
    results = await env.APP_DB.batch(statements)
    rows = results[-1].get('results', []) if isinstance(results, list) and results else []
    if len(rows) != 1:
        raise LookupError('Chat admission no longer owns its quota reservation')
    row = rows[0]
    return ChatTarget(target.uid, str(row['id']), target.app_id, str(row['clear_epoch']))


async def persist_chat_messages(env, target, messages, created_at, settlement=None):
    """Commit an entire provider result only while its selected owner survives.

    Clear rotates the epoch; delete removes the owner. Neither can be undone by
    an in-flight provider completion. D1 batch serializes the check and writes.
    Old sessions retain the migration's empty epoch until their first clear.
    """
    if target.new or target.session_id is None or not messages:
        raise ValueError('chat commit requires an admitted session and messages')
    for message in messages:
        if message.get('chat_session_id') != target.session_id or message.get('app_id') != target.app_id:
            raise ValueError('chat message differs from selected owner')
    now = int(time.time())
    statements = []
    for ordinal, message in enumerate(messages):
        statements.append(
            env.APP_DB.prepare(
                'INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) '
                'SELECT uid, ?, ?, ?, ? FROM cf_chat_sessions '
                f'WHERE uid = ? AND id = ? AND {APP_SCOPE} IS ? AND clear_epoch = ?'
            ).bind(
                str(message['id']),
                target.app_id,
                created_at + ordinal,
                json.dumps(message, separators=(',', ':'), ensure_ascii=False),
                target.uid,
                target.session_id,
                target.app_id,
                target.epoch,
            )
        )
    statements.append(
        env.APP_DB.prepare(
            'UPDATE cf_chat_sessions SET updated_at = ?, message_count = message_count + ?, preview = ? '
            f'WHERE uid = ? AND id = ? AND {APP_SCOPE} IS ? AND clear_epoch = ?'
        ).bind(
            now,
            len(messages),
            str(messages[-1]['text'])[:100],
            target.uid,
            target.session_id,
            target.app_id,
            target.epoch,
        )
    )
    if settlement is not None:
        statements.append(settlement)
    # RETURNING/SELECT results, not D1 changes (FTS triggers can alter that count).
    statements.append(
        env.APP_DB.prepare(
            'SELECT id FROM cf_chat_messages WHERE uid = ? AND id IN (' + ','.join('?' for _ in messages) + ')'
        ).bind(target.uid, *(str(message['id']) for message in messages))
    )
    results = await env.APP_DB.batch(statements)
    rows = results[-1].get('results', []) if isinstance(results, list) and results else []
    if {row.get('id') for row in rows} != {message['id'] for message in messages}:
        raise LookupError('Chat session changed before completion')
