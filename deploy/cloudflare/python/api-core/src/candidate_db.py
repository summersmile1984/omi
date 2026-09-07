"""Generation-fenced D1 transaction snapshots for canonical Candidate writers."""

from datetime import datetime, timezone
import json

from candidate_kernel_policy import CandidateConflictError, CandidateGenerationMismatchError

_TABLES = {
    'candidates': ('cf_candidates', 'candidate_id'),
    'aliases': ('cf_candidate_aliases', 'key_hash'),
    'claims': ('cf_candidate_claims', 'semantic_id'),
    'integrations': ('cf_candidate_integration_outbox', 'outbox_id'),
    'attention': ('cf_task_attention_overrides', 'override_id'),
    'recommendations': ('cf_task_recommendation_heads', 'head_id'),
    'snapshot_receipts': ('cf_task_snapshot_receipts', 'receipt_id'),
    'recurrences': ('cf_task_recurrence_inbox', 'receipt_id'),
}
_FLAT_TABLES = {
    'tasks': ('cf_action_items', 'id'),
    'workstreams': ('cf_workstreams', 'id'),
    'goals': ('cf_goals', 'id'),
    'interventions': ('cf_task_interventions', 'intervention_id'),
    'feedback': ('cf_task_feedback', 'feedback_id'),
    'recommendation_jobs': ('cf_task_intelligence_jobs', 'job_id'),
    'context_snapshots': ('cf_task_context_snapshots', 'scope_key'),
    'open_loop_snapshots': ('cf_task_open_loop_snapshots', 'scope_key'),
    'outcomes': ('cf_task_outcomes', 'outcome_id'),
    'artifacts': ('cf_workstream_artifacts', 'artifact_id'),
}
_TASK_PATCH = frozenset(
    {
        'capture_confidence',
        'ownership_confidence',
        'provenance',
        'updated_at',
        'due_confidence',
        'priority',
        'description',
        'status',
        'completed',
        'completed_at',
        'goal_id',
        'workstream_id',
        'owner',
        'due_at',
        'recurrence_rule',
        'recurrence_parent_id',
        'superseded_by',
        'account_generation',
    }
)


class CandidateSnapshotChanged(CandidateConflictError):
    """Retry the complete read/plan transaction against the new authoritative state."""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def task_value(row):
    if row is None:
        return None
    value = dict(row)
    value['provenance'] = json.loads(value.pop('provenance_json'))
    for field in ('created_at', 'updated_at', 'due_at', 'completed_at', 'export_date'):
        if value.get(field) is not None:
            value[field] = datetime.fromtimestamp(value[field], timezone.utc)
    for field in ('completed', 'deleted', 'is_locked', 'exported', 'sync_requested'):
        value[field] = bool(value[field])
    return value


async def account_generation(env, uid):
    row = (
        await env.APP_DB.prepare(
            'SELECT COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid=?),0) AS generation, '
            '(EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=?) '
            'OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=?)) AS deleted'
        )
        .bind(uid, uid, uid)
        .first()
    )
    if row['deleted'] or type(row['generation']) is not int or row['generation'] < 0:
        raise CandidateGenerationMismatchError('account generation unavailable')
    return row['generation']


class CandidateTransaction:
    """One request's observed rows and prepared writes; no network work in commit."""

    def __init__(self, env, uid, generation):
        self.db, self.uid, self.generation = env.APP_DB, uid, generation
        self.observed = {group: {} for group in (*_TABLES, *_FLAT_TABLES)}
        self.statements = []

    async def record(self, group, key):
        table, column = _TABLES[group]
        if key not in self.observed[group]:
            row = (
                await self.db.prepare(f'SELECT record_json FROM {table} WHERE uid=? AND {column}=?')
                .bind(self.uid, key)
                .first()
            )
            self.observed[group][key] = row['record_json'] if row else None
        return self.observed[group][key]

    async def row(self, group, key):
        table, column = _FLAT_TABLES[group]
        if key not in self.observed[group]:
            self.observed[group][key] = (
                await self.db.prepare(f'SELECT * FROM {table} WHERE uid=? AND {column}=?').bind(self.uid, key).first()
            )
        value = self.observed[group][key]
        return dict(value) if value is not None else None

    async def task(self, key):
        return task_value(await self.row('tasks', key))

    def insert_task(self, key, task):
        if key not in self.observed['tasks'] or self.observed['tasks'][key] is not None:
            raise ValueError('Candidate task creation requires an observed absent identity')
        fields = _TASK_PATCH | {
            'id',
            'source',
            'candidate_id',
            'idempotency_key',
            'sort_order',
            'indent_level',
            'created_at',
        }
        payload = {field: value for field, value in task.items() if field != 'task_id'}
        if payload.get('id') != key or not set(payload).issubset(fields):
            raise ValueError('invalid Candidate task storage')
        self._insert('cf_action_items', payload, json_fields={'provenance'})

    def insert_workstream(self, key, workstream, event):
        if key not in self.observed['workstreams'] or self.observed['workstreams'][key] is not None:
            raise ValueError('Candidate workstream creation requires an observed absent identity')
        payload = dict(workstream)
        if payload.pop('workstream_id') != key or event['workstream_id'] != key:
            raise ValueError('Candidate workstream identity mismatch')
        payload['id'] = key
        self._insert('cf_workstreams', payload)
        self._insert('cf_workstream_events', event, json_fields={'evidence_refs'})

    def _insert(self, table, payload, *, json_fields=frozenset()):
        physical = self._physical(payload, json_fields=json_fields)
        self.statements.append(
            self.db.prepare(
                f'INSERT INTO {table}(uid,'
                + ','.join(physical)
                + ') VALUES ('
                + ','.join('?' for _ in range(1 + len(physical)))
                + ')'
            ).bind(self.uid, *physical.values())
        )

    @staticmethod
    def _physical(payload, *, json_fields=frozenset()):
        physical = {}
        for field, value in payload.items():
            if field in json_fields:
                field, value = field + '_json', encoded(value)
            elif isinstance(value, datetime):
                value = int(value.timestamp())
            elif isinstance(value, bool):
                value = int(value)
            elif hasattr(value, 'value'):
                value = value.value
            physical[field] = value
        return physical

    def put(self, group, key, record):
        if key not in self.observed[group]:
            raise ValueError('Candidate write requires an observed identity')
        table, column = _TABLES[group]
        self.statements.append(
            self.db.prepare(
                f'INSERT INTO {table}(uid,{column},record_json) VALUES (?,?,?) '
                f'ON CONFLICT(uid,{column}) DO UPDATE SET record_json=excluded.record_json'
            ).bind(self.uid, key, encoded(record))
        )

    def patch_task(self, key, patch):
        if key not in self.observed['tasks'] or not self.observed['tasks'][key]:
            raise ValueError('Candidate task update requires an existing observed task')
        if not set(patch).issubset(_TASK_PATCH):
            raise ValueError('unsupported Candidate task patch')
        physical = self._physical(patch, json_fields={'provenance'})
        self.statements.append(
            self.db.prepare(
                'UPDATE cf_action_items SET ' + ','.join(field + '=?' for field in physical) + ' WHERE uid=? AND id=?'
            ).bind(*physical.values(), self.uid, key)
        )

    def prepared_batch(self):
        """Allow domain owners to include this guarded write in a larger D1 batch."""
        groups = tuple(self.observed)
        snapshots = [
            encoded([{'id': key, 'before': before} for key, before in self.observed[group].items()]) for group in groups
        ]
        guard = self.db.prepare(
            'INSERT INTO cf_candidate_write_guard(uid,account_generation,'
            + ','.join(group + '_json' for group in groups)
            + ') VALUES ('
            + ','.join('?' for _ in range(2 + len(groups)))
            + ')'
        ).bind(self.uid, self.generation, *snapshots)
        clear = self.db.prepare('DELETE FROM cf_candidate_write_guard WHERE uid=?').bind(self.uid)
        return [guard, *self.statements, clear]

    async def commit(self):
        try:
            await self.db.batch(self.prepared_batch())
        except Exception as error:
            if 'candidate_generation_changed' in str(error) or 'account deletion fence' in str(error):
                raise CandidateGenerationMismatchError('account generation changed') from error
            if 'candidate_snapshot_changed' in str(error):
                raise CandidateSnapshotChanged('Candidate state changed during commit') from error
            raise
