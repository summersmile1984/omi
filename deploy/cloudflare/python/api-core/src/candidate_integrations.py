"""Generation/lease-fenced accepted-task side effects; external I/O belongs to Jobs."""

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from candidate_attention import transaction
from candidate_db import CandidateSnapshotChanged
from candidate_kernel_policy import CandidateGenerationMismatchError
from integration_kernel import CANDIDATE_INTEGRATION_POLICY, _build_apple_reminders_sync_message
from integration_queue_policy import ProcessOutcome, decide_attempt
from fallback import record_fallback

PLATFORMS = {'apple_reminders', 'todoist', 'asana', 'google_tasks', 'clickup'}
READY_SQL = "status IN ('pending','failed','processing') AND " + (
    "(julianday(json_extract(record_json,'$.available_at')) IS NULL OR "
    "julianday(json_extract(record_json,'$.available_at'))<=julianday(?)) AND "
    "(status!='processing' OR julianday(json_extract(record_json,'$.claimed_at')) IS NULL OR "
    "julianday(json_extract(record_json,'$.claimed_at'))<=julianday(?))"
)


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    return datetime.fromisoformat(value) if value else None


def fallback(*, exhausted=False):
    record_fallback(
        from_mode='candidate_integration',
        to_mode='candidate_integration_retry',
        reason='dependency_unavailable',
        outcome='exhausted' if exhausted else 'degraded',
    )


async def retry(operation):
    for attempt in range(3):
        try:
            return await operation()
        except CandidateSnapshotChanged:
            if attempt == 2:
                raise


async def load(env, uid, generation, identity):
    tx = await transaction(env, uid, generation)
    raw = await tx.record('integrations', identity)
    value = json.loads(raw) if raw is not None else None
    if value is not None and value.get('account_generation') != generation:
        raise CandidateGenerationMismatchError('integration generation mismatch')
    return tx, value


async def claim(env, uid, generation, identity):
    async def attempt():
        tx, value = await load(env, uid, generation, identity)
        current = now()
        if value is None or value['status'] in {'completed', 'suppressed', 'dead_letter'}:
            return None
        try:
            if (
                value.get('candidate_id') != identity
                or not isinstance(value.get('task_id'), str)
                or not value['task_id']
            ):
                raise ValueError('malformed integration identity')
            available = timestamp(value.get('available_at'))
            claimed = timestamp(value.get('claimed_at'))
            if available and available > current:
                return None
            if value['status'] == 'processing' and claimed and claimed + timedelta(seconds=300) > current:
                return None
            count = int(value.get('attempt_count') or 0)
        except (TypeError, ValueError):
            tx.put(
                'integrations',
                identity,
                dict(
                    value,
                    status='dead_letter',
                    lease_token=None,
                    dead_letter_reason='malformed',
                    last_error_text='malformed_integration',
                    updated_at=current.isoformat(),
                ),
            )
            await tx.commit()
            return None
        token = uuid4().hex
        tx.put(
            'integrations',
            identity,
            dict(
                value,
                status='processing',
                lease_token=token,
                attempt_count=count + 1,
                dispatch_started_at=None,
                claimed_at=current.isoformat(),
                updated_at=current.isoformat(),
            ),
        )
        await tx.commit()
        return token

    return await retry(attempt)


async def schedule(env, uid, generation, identity):
    if getattr(env, 'JOBS', None) is None:
        fallback()
        return False
    token = await claim(env, uid, generation, identity)
    if token is None:
        return False
    try:
        await env.JOBS.send(
            {
                'uid': uid,
                'jobId': identity,
                'kind': 'candidate_integration',
                'payload': {'account_generation': generation, 'lease_token': token},
            }
        )
        return True
    except Exception:
        # The committed lease/outbox survives a lost hint; Cron can reclaim it.
        fallback()
        return False


async def schedule_after_accept(env, uid, generation, identity):
    try:
        await schedule(env, uid, generation, identity)
    except Exception:
        fallback()


async def drain(env, uid, generation, limit):
    tx = await transaction(env, uid, generation)
    current = now()
    rows = (
        await env.APP_DB.prepare(
            'SELECT outbox_id FROM cf_candidate_integration_outbox WHERE uid=? AND account_generation=? AND '
            + READY_SQL
            + " ORDER BY json_extract(record_json,'$.created_at'),outbox_id LIMIT ?"
        )
        .bind(uid, generation, current.isoformat(), (current - timedelta(seconds=300)).isoformat(), limit)
        .all()
    )
    await tx.commit()
    scheduled = 0
    for row in rows['results']:
        try:
            scheduled += int(await schedule(env, uid, generation, row['outbox_id']))
        except CandidateGenerationMismatchError:
            raise
        except Exception:
            fallback()
    return scheduled


def leased(value, token, *, fresh=False):
    return (
        value is not None
        and value['status'] == 'processing'
        and value.get('lease_token') == token
        and (not fresh or timestamp(value['claimed_at']) + timedelta(seconds=300) > now())
    )


async def prepare(env, uid, generation, identity, token, platform):
    async def attempt():
        tx, value = await load(env, uid, generation, identity)
        if not leased(value, token, fresh=True) or value.get('dispatch_started_at'):
            return {'status': 'obsolete'}
        task = await tx.task(value['task_id'])
        if task is None or task['deleted'] or task['account_generation'] != generation:
            return {'status': 'task_missing'}
        tx.put('integrations', identity, dict(value, dispatch_started_at=now().isoformat()))
        output = {
            'status': 'ready',
            'task': {
                'id': task['id'],
                'description': task['description'],
                'due_at': task['due_at'].isoformat() if task['due_at'] else None,
                'exported': task['exported'],
            },
        }
        if platform == 'apple_reminders':
            tx.patch_task(task['id'], {'sync_requested': True})
            tag, data = _build_apple_reminders_sync_message(uid, [task])
            output['push'] = {'tag': tag, 'data': data}
        await tx.commit()
        return output

    return await retry(attempt)


async def settle(env, uid, generation, identity, token, *, succeeded, platform, external_id=None):
    async def attempt():
        tx, value = await load(env, uid, generation, identity)
        if not leased(value, token):
            return {'status': 'obsolete'}
        current = now()
        patch = dict(lease_token=None, updated_at=current.isoformat())
        if succeeded:
            patch.update(
                status='completed',
                completed_at=current.isoformat(),
                last_error_text=None,
                dead_letter_reason=None,
                external_task_id=external_id,
            )
            if platform and platform != 'apple_reminders':
                task = await tx.task(value['task_id'])
                if task and not task['deleted'] and task['account_generation'] == generation and not task['exported']:
                    tx.patch_task(task['id'], {'exported': True, 'export_platform': platform, 'export_date': current})
        else:
            decision = decide_attempt(
                attempt_count=max(value['attempt_count'], 1),
                outcome=ProcessOutcome.retry('integration_not_synced', reason='integration_failed'),
                policy=CANDIDATE_INTEGRATION_POLICY,
                now=current,
            )
            patch.update(
                status='dead_letter' if decision.terminal else 'failed',
                completed_at=None,
                last_error_text=decision.error_text,
                dead_letter_reason=decision.reason if decision.terminal else None,
            )
            if decision.available_at is not None:
                patch['available_at'] = decision.available_at.isoformat()
        tx.put('integrations', identity, dict(value, **patch))
        await tx.commit()
        if not succeeded:
            fallback(exhausted=patch['status'] == 'dead_letter')
        return {'status': patch['status']}

    return await retry(attempt)
