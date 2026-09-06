"""Private report service; Jobs owns the public ADMIN_KEY gate and attribution."""

from datetime import datetime
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from feedback_context import get_event, hydrate_context, resolve_context
from feedback_policy import previous_utc_day
from feedback_reports import ReportBusy, get_report, run_report
from internal_auth import verify_request_context


def _internal_admin(request: Request):
    context = verify_request_context(
        request.headers.get('x-omi-auth-context'),
        request.headers.get('x-omi-internal-signature'),
        getattr(request.scope['env'], 'INTERNAL_ASSERTION_SECRET', None),
        audience='api-core',
        method=request.method,
        path=request.url.path,
    )
    if not context or context.get('authority') != 'internal':
        raise HTTPException(403, 'Invalid feedback service identity')
    if context['uid'] == 'feedback-scheduler':
        if request.method == 'POST' and request.url.path == '/internal/feedback/ensure-yesterday':
            return context
    elif re.fullmatch(r'feedback-admin:[0-9a-f]{8}/(?:[0-9a-f]{16}|unattributed)', context['uid']):
        return context
    raise HTTPException(403, 'Invalid feedback service identity')


router = APIRouter(prefix='/internal/feedback', dependencies=[Depends(_internal_admin)], include_in_schema=False)


def _parse_date(value: str):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise HTTPException(400, 'date must be YYYY-MM-DD')


def _unavailable():
    return JSONResponse({'detail': 'Feedback reports unavailable'}, status_code=503)


@router.get('/reports')
async def list_feedback_reports(request: Request, limit: int = Query(default=30, ge=1, le=90)):
    try:
        rows = (
            await request.scope['env']
            .APP_DB.prepare(
                'SELECT date FROM cf_feedback_reports WHERE metadata_json IS NOT NULL ORDER BY date DESC LIMIT ?'
            )
            .bind(limit)
            .all()
        )
        return {'dates': [row['date'] for row in rows.get('results', [])]}
    except Exception:
        return _unavailable()


@router.get('/reports/{report_date}')
async def get_feedback_report(request: Request, report_date: str):
    day = _parse_date(report_date).isoformat()
    try:
        report = await get_report(request.scope['env'], day)
    except Exception:
        return _unavailable()
    if report is None:
        raise HTTPException(404, f'No feedback report for {day}')
    return report


async def _generate(request: Request, day, *, only_missing: bool = False):
    try:
        report = await run_report(request.scope['env'], day, only_missing=only_missing)
        return {'date': report.date, 'total_negative': report.total_negative, 'truncated': report.truncated}
    except ReportBusy:
        return JSONResponse({'detail': 'Feedback report generation in progress'}, status_code=409)
    except Exception:
        return _unavailable()


@router.post('/reports/{report_date}/generate')
async def generate_feedback_report(request: Request, report_date: str):
    return await _generate(request, _parse_date(report_date))


@router.post('/reports/generate-yesterday')
async def generate_yesterdays_feedback_report(request: Request):
    return await _generate(request, previous_utc_day())


@router.post('/ensure-yesterday')
async def ensure_yesterdays_feedback_report(request: Request):
    return await _generate(request, previous_utc_day(), only_missing=True)


@router.get('/events/{event_id}/context')
async def get_feedback_event_context(request: Request, event_id: str, report_date: str | None = None):
    day = _parse_date(report_date).isoformat() if report_date else None
    env = request.scope['env']
    try:
        event = await get_event(env, event_id)
        if event is None:
            raise HTTPException(404, f'No feedback event {event_id}')
        report = await get_report(env, day) if day else None
        pointer = (
            next((entry.context for entry in report.entries if entry.event.id == event_id), None) if report else None
        )
        if pointer is None:
            pointer = await resolve_context(env, event)
        return await hydrate_context(env, event, pointer)
    except HTTPException:
        raise
    except Exception:
        return _unavailable()
