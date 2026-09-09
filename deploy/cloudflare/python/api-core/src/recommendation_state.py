"""D1 reads feeding the original bounded What Matters Now product-state contract."""

from datetime import datetime, timezone
import json

from pydantic import ValidationError
from candidate_db import account_generation as current_generation, task_value
from candidate_kernel_policy import CandidateGenerationMismatchError
from candidate_kernel_recommendation import (
    NormalizedContextSnapshot,
    OpenLoopSnapshot,
    WhatMattersNowProjection,
    DecisionRecord,
)
from candidate_read import parse_read
from fallback import record_fallback


def parse_model(model, value):
    try:
        return model.model_validate_json(value) if isinstance(value, str) else model.model_validate(value)
    except ValidationError:
        record_fallback(from_mode='none', to_mode='none', reason='malformed_doc', outcome='degraded')
        return None


def logical_row(row):
    result = {}
    for key, value in row.items():
        if key == 'uid':
            continue
        if key.endswith('_json'):
            result[key[:-5]] = json.loads(value) if value is not None else None
        elif (
            key in {'created_at', 'updated_at', 'next_review_at', 'due_at', 'resolved_at', 'expires_at', 'completed_at'}
            and value is not None
        ):
            result[key] = datetime.fromtimestamp(value, timezone.utc)
        else:
            result[key] = value
    return result


def candidate_value(record):
    value = record.model_dump(mode='json')
    for key in ('created_at', 'expires_at', 'resolved_at'):
        if value.get(key):
            value[key] = datetime.fromisoformat(value[key])
    for change in (value.get('task_change'), (value.get('workstream_proposal') or {}).get('anchor_task')):
        if isinstance(change, dict) and change.get('due_at'):
            change['due_at'] = datetime.fromisoformat(change['due_at'])
    return value


class RecommendationReader:
    def __init__(self, env, uid, generation, device_scope):
        self.env, self.uid, self.generation, self.device_scope = env, uid, generation, device_scope

    async def guard(self, uid, generation):
        if uid != self.uid or generation != self.generation or await current_generation(self.env, uid) != generation:
            raise CandidateGenerationMismatchError('recommendation account generation changed')

    async def all(self, query, *args):
        return (await self.env.APP_DB.prepare(query).bind(*args).all())['results']

    async def load_canonical_product_state(self, uid, *, account_generation):
        await self.guard(uid, account_generation)
        state = {}
        for name, table, limit in [
            ('tasks', 'cf_action_items', 500),
            ('goals', 'cf_goals', 100),
            ('workstreams', 'cf_workstreams', 200),
        ]:
            predicate = ' AND deleted=0' if name == 'tasks' else ''
            rows = await self.all(
                f'SELECT * FROM {table} WHERE uid=? AND account_generation=?{predicate} ORDER BY id LIMIT ?',
                uid,
                account_generation,
                limit,
            )
            state[name] = [task_value(row) if name == 'tasks' else logical_row(row) for row in rows]
        rows = await self.all(
            'SELECT record_json FROM cf_candidates WHERE uid=? AND account_generation=? ORDER BY candidate_id LIMIT 200',
            uid,
            account_generation,
        )
        state['candidates'] = [
            candidate_value(record) for row in rows if (record := parse_read(row['record_json'])) is not None
        ]
        state['artifacts'], state['workstream_events'] = [], []
        for stream in state['workstreams']:
            for name, table, order, per_stream in [
                ('artifacts', 'cf_workstream_artifacts', 'artifact_id', 100),
                ('workstream_events', 'cf_workstream_events', 'sequence DESC', 20),
            ]:
                remaining = 200 - len(state[name])
                if remaining <= 0:
                    continue
                rows = await self.all(
                    f'SELECT * FROM {table} WHERE uid=? AND workstream_id=? ORDER BY {order} LIMIT ?',
                    uid,
                    stream['id'],
                    min(remaining, per_stream),
                )
                state[name].extend(logical_row(row) for row in rows)
        await self.guard(uid, account_generation)
        return state

    async def list_active_override_dedupe_keys(self, uid, *, now, account_generation):
        await self.guard(uid, account_generation)
        rows = await self.all(
            'SELECT record_json FROM cf_task_attention_overrides WHERE uid=? AND account_generation=? AND expires_at>=?',
            uid,
            account_generation,
            int(now.timestamp()),
        )
        return {
            record['dedupe_key']
            for row in rows
            if (record := json.loads(row['record_json'])).get('dedupe_key')
            and datetime.fromisoformat(record['expires_at']) > now
        }

    async def get_context_snapshot(self, uid, device_id, *, now, account_generation):
        from recommendation_snapshots import read_snapshots

        await self.guard(uid, account_generation)
        values = await read_snapshots(self.env, uid, account_generation, device_id, now=now, open_loop=False)
        return values[0] if values else None

    async def list_open_loop_snapshots(self, uid, *, device_id, now, account_generation):
        from recommendation_snapshots import read_snapshots

        await self.guard(uid, account_generation)
        return await read_snapshots(self.env, uid, account_generation, device_id, now=now, open_loop=True)

    async def get_decisions(self, uid, evaluation_id, *, device_scope, account_generation):
        await self.guard(uid, account_generation)
        rows = await self.all(
            'SELECT decisions_json FROM cf_task_evaluations WHERE uid=? AND evaluation_id=? AND device_id=? AND account_generation=?',
            uid,
            evaluation_id,
            device_scope,
            account_generation,
        )
        values = json.loads(rows[0]['decisions_json']) if rows else []
        return sorted(
            [record for value in values if (record := parse_model(DecisionRecord, value)) is not None],
            key=lambda record: record.subject_id,
        )

    async def get_evaluation_projection(self, uid, evaluation_id, *, device_scope, now, account_generation):
        await self.guard(uid, account_generation)
        rows = await self.all(
            'SELECT projection_json FROM cf_task_evaluations WHERE uid=? AND evaluation_id=? AND device_id=? AND account_generation=?',
            uid,
            evaluation_id,
            device_scope,
            account_generation,
        )
        record = parse_model(WhatMattersNowProjection, rows[0]['projection_json']) if rows else None
        return (
            record if record is not None and record.evaluation_id == evaluation_id and record.expires_at > now else None
        )
