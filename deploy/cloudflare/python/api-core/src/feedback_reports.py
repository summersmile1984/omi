"""Bounded daily report generation with fenced atomic D1 publication."""

from datetime import date, datetime, timezone
import json
import time
import uuid

from fallback import record_fallback
from feedback_contract import FeedbackContextPointer, FeedbackEvent, FeedbackReport, FeedbackReportEntry
from feedback_context import LIVE_EVENT, resolve_context
from feedback_policy import (
    MAX_REPORT_ENTRIES,
    MAX_REPORT_DOCUMENT_BYTES,
    RAW_FETCH_LIMIT,
    _collapse_per_target,
    _day_bounds,
    _entry_bytes,
)

LEASE_SECONDS = 300


class ReportBusy(Exception):
    pass


async def get_report(env, day: str) -> FeedbackReport | None:
    # Aggregate and entries come from one read snapshot, never two generations.
    row = (
        await env.APP_DB.prepare(
            'SELECT r.metadata_json, (SELECT json_group_array(json(p.entry_json)) '
            'FROM (SELECT entry_json, event_id FROM cf_feedback_report_entries WHERE report_date = r.date ORDER BY position) p '
            'JOIN cf_feedback_events e ON e.id = p.event_id WHERE ' + LIVE_EVENT + ') AS entries_json '
            'FROM cf_feedback_reports r WHERE r.date = ? AND r.metadata_json IS NOT NULL'
        )
        .bind(day)
        .first()
    )
    if not row:
        return None
    return FeedbackReport.model_validate(
        {**json.loads(row['metadata_json']), 'entries': json.loads(row['entries_json'])}
    )


async def generate_report(env, day: date) -> FeedbackReport:
    start, end = _day_bounds(day)
    raw = (
        await env.APP_DB.prepare(
            'SELECT e.event_json FROM cf_feedback_events e WHERE e.value = -1 AND e.created_at >= ? '
            'AND e.created_at < ? AND ' + LIVE_EVENT + ' ORDER BY e.created_at, e.id LIMIT ?'
        )
        .bind(start.timestamp(), end.timestamp(), RAW_FETCH_LIMIT + 1)
        .all()
    )
    values = raw.get('results', [])
    events = _collapse_per_target(
        [FeedbackEvent.model_validate_json(row['event_json']) for row in values[:RAW_FETCH_LIMIT]]
    )
    report = FeedbackReport(
        date=day.isoformat(),
        generated_at=datetime.now(timezone.utc),
        total_negative=len(events),
        truncated=len(values) > RAW_FETCH_LIMIT or len(events) > MAX_REPORT_ENTRIES,
    )
    for event in events:
        for counts, key in (
            (report.counts_by_surface, event.surface.value),
            (report.counts_by_reason, event.reason.value if event.reason else 'not_captured'),
            (report.counts_by_platform, event.platform or 'unknown'),
        ):
            counts[key] = counts.get(key, 0) + 1
    used = 0
    for event in events[:MAX_REPORT_ENTRIES]:
        try:
            pointer = await resolve_context(env, event)
        except Exception:
            record_fallback(
                from_mode='none', to_mode='metadata_only', reason='dependency_unavailable', outcome='degraded'
            )
            pointer = FeedbackContextPointer(
                event_id=event.id,
                uid=event.uid,
                target_kind=event.target_kind,
                target_id=event.target_id,
                resolution_error='resolution_failed',
            )
        entry = FeedbackReportEntry(event=event, context=pointer)
        used += _entry_bytes(entry)
        if used > MAX_REPORT_DOCUMENT_BYTES and report.entries:
            report.truncated = True
            break
        report.entries.append(entry)
    return report


async def run_report(env, day: date, *, only_missing: bool = False) -> FeedbackReport:
    day_id = day.isoformat()
    if only_missing:
        existing = await get_report(env, day_id)
        if existing:
            return existing
    token, now = str(uuid.uuid4()), int(time.time())
    acquired = (
        await env.APP_DB.prepare(
            'INSERT INTO cf_feedback_reports (date, lease_token, lease_expires_at) VALUES (?, ?, ?) '
            'ON CONFLICT(date) DO UPDATE SET lease_token = excluded.lease_token, lease_expires_at = excluded.lease_expires_at '
            'WHERE cf_feedback_reports.lease_expires_at <= ? '
            + ('AND cf_feedback_reports.metadata_json IS NULL ' if only_missing else '')
            + 'RETURNING lease_token'
        )
        .bind(day_id, token, now + LEASE_SECONDS, now)
        .first()
    )
    if not acquired:
        raise ReportBusy()
    try:
        report = await generate_report(env, day)
        metadata = report.model_dump_json(exclude={'entries'})
        entries = json.dumps([entry.model_dump(mode='json') for entry in report.entries])
        # Every statement checks the same current lease. Late completion cannot
        # delete a newer report, and erasure cannot republish cached event data.
        admitted = (
            'EXISTS (SELECT 1 FROM cf_feedback_reports WHERE date = ? AND lease_token = ? AND lease_expires_at > ?)'
        )
        publication_time = int(time.time())
        results = await env.APP_DB.batch(
            [
                env.APP_DB.prepare('DELETE FROM cf_feedback_report_entries WHERE report_date = ? AND ' + admitted).bind(
                    day_id, day_id, token, publication_time
                ),
                env.APP_DB.prepare(
                    'INSERT INTO cf_feedback_report_entries (report_date, position, event_id, uid, entry_json) '
                    "SELECT ?, CAST(j.key AS INTEGER), e.id, e.uid, j.value FROM json_each(?) j "
                    "JOIN cf_feedback_events e ON e.id = json_extract(j.value, '$.event.id') "
                    "AND e.uid = json_extract(j.value, '$.event.uid') WHERE " + LIVE_EVENT + ' AND ' + admitted
                ).bind(day_id, entries, day_id, token, publication_time),
                env.APP_DB.prepare(
                    'UPDATE cf_feedback_reports SET metadata_json = ?, lease_token = NULL, lease_expires_at = 0 '
                    'WHERE date = ? AND lease_token = ? AND lease_expires_at > ?'
                ).bind(metadata, day_id, token, publication_time),
            ]
        )
        if not results[-1].get('meta', {}).get('changes'):
            raise ReportBusy()
        # Return the actually published set, including any concurrent erasures.
        published = await get_report(env, day_id)
        if not published:
            raise RuntimeError('feedback report publication unavailable')
        return published
    finally:
        await env.APP_DB.prepare(
            'UPDATE cf_feedback_reports SET lease_token = NULL, lease_expires_at = 0 WHERE date = ? AND lease_token = ?'
        ).bind(day_id, token).run()
