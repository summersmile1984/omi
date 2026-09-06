"""Upstream report contracts through private HTTP routes and real D1 SQL."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest

import feedback_admin_routes
import feedback_reports
from feedback_context import get_event, hydrate_context, resolve_context
from feedback_contract import FeedbackEvent, FeedbackReport
from feedback_policy import MAX_REPORT_DOCUMENT_BYTES, _day_bounds
from test_feedback_ledger import Harness

ADMIN = 'feedback-admin:01234567/89abcdef01234567'


def harness():
    h = Harness()
    h.app.include_router(feedback_admin_routes.router)
    return h


async def admin(h, method, path, **kwargs):
    return await h.request(method, '/internal/feedback' + path, uid=ADMIN, authority='internal', **kwargs)


async def rated(h, message_id='answer', reason=None):
    response = await h.request(
        'PATCH', f'/v2/desktop/messages/{message_id}/rating', body={'rating': -1, 'reason': reason}
    )
    assert response.status_code == 200
    return h.events()[-1]


def insert_event(h, day, *, target_id=None, created_at=None, reason=None, comment=None, uid='owner'):
    event = FeedbackEvent(
        id=str(uuid.uuid4()),
        uid=uid,
        surface='chat_text',
        target_kind='chat_message',
        target_id=target_id or str(uuid.uuid4()),
        value=-1,
        created_at=created_at or _day_bounds(day)[0],
        reason=reason,
        comment=comment,
    )
    h.db.connection.execute(
        'INSERT INTO cf_feedback_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (
            event.id,
            uid,
            event.surface.value,
            event.target_kind.value,
            event.target_id,
            event.value,
            event.created_at.timestamp(),
            event.model_dump_json(),
        ),
    )
    return event


def test_daily_report_collapses_votes_keeps_reason_and_hydrates_only_on_demand():
    async def scenario():
        h = harness()
        h.message(message_id='before', created_at=50, sender='human')
        h.message()
        h.message(message_id='after', created_at=110, sender='human', session_id='new-session')
        h.message(uid='other', message_id='secret', created_at=110)
        reasoned = await rated(h, reason='not_useful')
        await rated(h)  # The out-of-order bare tap must not erase the reason.
        day = reasoned.created_at.date().isoformat()
        h.db.queries.clear()
        generated = await admin(h, 'POST', f'/reports/{day}/generate')
        assert generated.status_code == 200, generated.text
        assert generated.json() == {'date': day, 'total_negative': 1, 'truncated': False}
        # Verify the actual storage read projection, not a scrape of source code.
        message_reads = [sql for sql in h.db.queries if 'FROM cf_chat_messages' in sql]
        assert message_reads and all(
            "'$.text'" not in sql and 'SELECT message_json' not in sql for sql in message_reads
        )
        report_response = await admin(h, 'GET', f'/reports/{day}')
        report = FeedbackReport.model_validate(report_response.json())
        assert report.counts_by_reason == {'not_useful': 1}
        assert report.entries[0].event.id == reasoned.id
        assert [t.message_id for t in report.entries[0].context.turns] == ['before', 'answer', 'after']
        assert 'PRIVATE' not in report_response.text
        unpadded = f'{reasoned.created_at.year}-{reasoned.created_at.month}-{reasoned.created_at.day}'
        assert (await admin(h, 'GET', f'/reports/{unpadded}')).json() == report_response.json()
        assert (await admin(h, 'GET', '/reports?limit=1')).json() == {'dates': [day]}
        context = await admin(h, 'GET', f'/events/{reasoned.id}/context?report_date={day}')
        assert context.status_code == 200
        assert [t['text'] for t in context.json()['turns']] == ['PRIVATE TARGET TEXT'] * 3
        stored = h.db.connection.execute('SELECT entry_json FROM cf_feedback_report_entries').fetchone()[0]
        assert 'PRIVATE' not in stored

    asyncio.run(scenario())


def test_private_service_rejects_user_assertions_missing_key_and_wrong_request_scope():
    async def scenario():
        h = harness()
        for uid, authority in [
            ('owner', 'better-auth'),
            (ADMIN, 'better-auth'),
            ('unrelated-service', 'internal'),
            ('feedback-scheduler', 'internal'),
            (None, 'internal'),
        ]:
            response = await h.request('GET', '/internal/feedback/reports', uid=uid, authority=authority)
            assert response.status_code == 403
        assert (await admin(h, 'GET', '/reports?limit=91')).status_code == 422
        assert (await admin(h, 'GET', '/reports/2026-02-30')).status_code == 400
        assert (await admin(h, 'GET', '/reports/2026-01-01')).status_code == 404
        assert (await admin(h, 'GET', '/events/absent/context')).status_code == 404

    asyncio.run(scenario())


def test_real_desktop_message_writes_use_wire_time_instead_of_history_sort_keys():
    async def scenario():
        h = harness()
        first = await h.request('POST', '/v2/desktop/messages', body={'sender': 'human', 'text': 'Before'})
        assert first.status_code == 200
        second = await h.request(
            'POST',
            '/v2/desktop/messages',
            body={
                'sender': 'ai',
                'text': 'Rated',
                'session_id': first.json()['session_id'],
            },
        )
        assert second.status_code == 200
        event = await rated(h, second.json()['id'])
        assert event.target_created_at == datetime.fromisoformat(second.json()['created_at'])
        pointer = await resolve_context(h.env, event)
        assert [turn.message_id for turn in pointer.turns] == [first.json()['id'], second.json()['id']]
        # Imported offset timestamps and sub-millisecond turns keep exact order.
        for index, stamp in enumerate(
            ['2026-09-06T10:00:00.000001+08:00', '2026-09-06T02:00:00.000002Z', '2026-09-06T02:00:00.000003+00:00']
        ):
            h.message(message_id=f'precise-{index}', created_at=1, session_id='precise')
            h.db.connection.execute(
                "UPDATE cf_chat_messages SET message_json=json_set(message_json, '$.created_at', ?) WHERE id=?",
                (stamp, f'precise-{index}'),
            )
        precise = await rated(h, 'precise-1')
        pointer = await resolve_context(h.env, precise)
        assert [turn.message_id for turn in pointer.turns] == ['precise-0', 'precise-1', 'precise-2']
        assert [turn.created_at.microsecond for turn in pointer.turns] == [1, 2, 3]

    asyncio.run(scenario())


def test_context_windows_bounds_missing_session_and_deleted_or_encrypted_turns():
    async def scenario():
        h = harness()
        h.message(created_at=100)
        for index in range(12):
            h.message(message_id=f'before-{index}', created_at=index, sender='human')
            h.message(message_id=f'after-{index}', created_at=101 + index, session_id='next-session')
        h.message(message_id='outside-window', created_at=401)
        h.message(message_id='different-before', created_at=99, session_id='other-session')
        event = await rated(h)
        pointer = await resolve_context(h.env, event)
        assert len(pointer.turns) == 21
        assert pointer.truncated_before and pointer.truncated_after
        assert pointer.follow_up_count == 10 and pointer.follow_up_window_seconds == 300
        ids = [turn.message_id for turn in pointer.turns]
        assert ids[:2] == ['before-2', 'before-3'] and ids[-1] == 'after-9'
        h.db.connection.execute("DELETE FROM cf_chat_messages WHERE uid='owner' AND id='before-2'")
        h.db.connection.execute(
            "UPDATE cf_chat_messages SET message_json=json_set(message_json, '$.data_protection_level', 'enhanced') WHERE id='before-3'"
        )
        h.db.connection.execute(
            "UPDATE cf_chat_messages SET message_json=json_set(message_json, '$.text', ?) WHERE id='answer'",
            ('x' * 5000,),
        )
        hydrated = await hydrate_context(h.env, event, pointer)
        assert hydrated.unavailable == ['before-2', 'before-3']
        assert next(t for t in hydrated.turns if t.message_id == 'answer').text == 'x' * 4000 + '… [truncated]'
        h.message(message_id='no-session', session_id=None)
        unknown = await rated(h, 'no-session')
        unknown_pointer = await resolve_context(h.env, unknown)
        assert unknown_pointer.resolution_error == 'preceding_turns_session_unknown'
        assert all(t.position != 'before' for t in unknown_pointer.turns)
        h.db.connection.execute("DELETE FROM cf_chat_messages WHERE id='no-session'")
        assert (await resolve_context(h.env, unknown)).resolution_error == 'rated_message_not_found'
        pointer.uid = 'other'
        with pytest.raises(ValueError, match='owner mismatch'):
            await hydrate_context(h.env, event, pointer)

    asyncio.run(scenario())


def test_report_limits_and_utc_day_exclusion_match_upstream():
    async def scenario():
        h = harness()
        day = datetime.now(timezone.utc).date()
        start, end = _day_bounds(day)
        insert_event(h, day, target_id='outside-before', created_at=start - timedelta(microseconds=1))
        insert_event(h, day, target_id='outside-after', created_at=end)
        for index in range(501):
            insert_event(h, day, target_id=f'item-{index}', created_at=start + timedelta(seconds=index))
        report = await feedback_reports.run_report(h.env, day)
        assert report.total_negative == 501 and report.truncated
        assert len(report.entries) == 500
        assert report.counts_by_reason == {'not_captured': 501}
        # 2001 raw votes on one artifact still declare a partial scan after collapse.
        h = harness()
        for _ in range(2001):
            insert_event(h, day, target_id='same')
        report = await feedback_reports.generate_report(h.env, day)
        assert report.total_negative == 1 and report.truncated and len(report.entries) == 1

    asyncio.run(scenario())


def test_byte_budget_caps_full_windows_without_losing_counts():
    async def scenario():
        h = harness()
        day = datetime.now(timezone.utc).date()
        for index in range(160):
            message_id = f'{index:04d}-' + 'x' * 200
            h.message(message_id=message_id, created_at=index)
            insert_event(h, day, target_id=message_id, comment='y' * 1000)
        report = await feedback_reports.generate_report(h.env, day)
        assert report.total_negative == 160 and report.truncated
        assert 0 < len(report.entries) < 160
        from feedback_policy import _entry_bytes

        assert sum(_entry_bytes(entry) for entry in report.entries) <= MAX_REPORT_DOCUMENT_BYTES

    asyncio.run(scenario())


def test_failed_generation_preserves_prior_report_and_stale_publisher_cannot_replace_it(monkeypatch):
    async def scenario():
        h = harness()
        h.message()
        event = await rated(h)
        day = event.created_at.date()
        original = await feedback_reports.run_report(h.env, day)
        h.db.fail_sql = 'INSERT INTO cf_feedback_report_entries'
        assert (await admin(h, 'POST', f'/reports/{day}/generate')).status_code == 503
        h.db.fail_sql = None
        assert await feedback_reports.get_report(h.env, str(day)) == original

        async def stale(env, requested_day):
            h.db.connection.execute(
                "UPDATE cf_feedback_reports SET lease_token='new-owner', lease_expires_at=9999999999"
            )
            return original

        monkeypatch.setattr(feedback_reports, 'generate_report', stale)
        assert (await admin(h, 'POST', f'/reports/{day}/generate')).status_code == 409
        assert await feedback_reports.get_report(h.env, str(day)) == original
        assert (await admin(h, 'POST', f'/reports/{day}/generate')).status_code == 409

    asyncio.run(scenario())


def test_erase_fences_live_and_cached_context_and_prevents_republication(monkeypatch):
    async def scenario():
        h = harness()
        h.message()
        event = await rated(h)
        day = event.created_at.date()
        report = await feedback_reports.run_report(h.env, day)
        h.db.connection.execute("INSERT INTO cf_account_deletion_tombstones VALUES ('owner', 1, 9999999999)")
        assert await get_event(h.env, event.id) is None
        assert (await admin(h, 'GET', f'/events/{event.id}/context?report_date={day}')).status_code == 404
        hidden = await feedback_reports.get_report(h.env, str(day))
        assert hidden.entries == []

        async def cached(env, requested_day):
            return report

        monkeypatch.setattr(feedback_reports, 'generate_report', cached)
        assert (await feedback_reports.run_report(h.env, day)).entries == []
        h.db.connection.execute("DELETE FROM cf_feedback_events WHERE uid='owner'")
        assert h.db.connection.execute('SELECT COUNT(*) FROM cf_feedback_report_entries').fetchone()[0] == 0

    asyncio.run(scenario())


def test_single_resolution_failure_keeps_the_report_and_scheduler_is_idempotent(monkeypatch, capsys):
    async def scenario():
        h = harness()
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        event = insert_event(h, yesterday)

        async def unavailable(env, item):
            raise RuntimeError('PRIVATE provider detail')

        monkeypatch.setattr(feedback_reports, 'resolve_context', unavailable)
        response = await h.request(
            'POST', '/internal/feedback/ensure-yesterday', uid='feedback-scheduler', authority='internal'
        )
        assert response.status_code == 200, response.text
        report = await feedback_reports.get_report(h.env, str(yesterday))
        assert report.entries[0].event.id == event.id
        assert report.entries[0].context.resolution_error == 'resolution_failed'
        h.db.queries.clear()
        assert (
            await h.request(
                'POST', '/internal/feedback/ensure-yesterday', uid='feedback-scheduler', authority='internal'
            )
        ).status_code == 200
        assert not any('INSERT' in query or 'UPDATE' in query for query in h.db.queries)

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert '"event":"fallback"' in output and 'PRIVATE' not in output
