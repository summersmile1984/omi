"""Original projection publication and a durable Workers inference lease in D1."""

from datetime import datetime, timezone
import json
import hashlib
import uuid

from candidate_db import CandidateTransaction, CandidateSnapshotChanged, encoded
from candidate_kernel_policy import CandidateConflictError, CandidateGenerationMismatchError
from candidate_kernel_recommendation import WhatMattersNowProjection
from fallback import record_fallback
from recommendation_kernel import _stable_id
from recommendation_state import RecommendationReader, parse_model

MAX_ATTEMPTS = 3
RETRY_SECONDS = 10
LEASE_SECONDS = 300
MAX_HISTORY = 24


class EvaluationUnavailable(RuntimeError):
    def __init__(self, job_id, status, retryable):
        super().__init__('recommendation evaluation unavailable')
        self.job_id, self.status, self.retryable = job_id, status, retryable


class RecommendationStore(RecommendationReader):
    def __init__(self, env, uid, generation, request, *, enqueue=True):
        super().__init__(env, uid, generation, request.device_id or 'global')
        self.request, self.enqueue = request, enqueue
        self.job_id, self.lease, self.model_receipt = None, None, None
        self.head_token = "none"

    def head_id(self):
        return _stable_id('projection', self.generation, self.device_scope)

    async def get_projection(self, uid, *, device_scope, now, include_expired=False, account_generation):
        await self.guard(uid, account_generation)
        if device_scope != self.device_scope:
            raise CandidateConflictError('projection device mismatch')
        raw = await CandidateTransaction(self.env, uid, account_generation).record('recommendations', self.head_id())
        self.head_token = hashlib.sha256(raw.encode()).hexdigest() if raw else 'none'
        data = json.loads(raw) if raw else None
        record = (
            parse_model(WhatMattersNowProjection, data['projection'])
            if data and data.get('account_generation') == account_generation
            else None
        )
        return record if record is not None and (include_expired or record.expires_at > now) else None

    async def _queue(self, job_id):
        if not self.enqueue:
            return
        try:
            await self.env.JOBS.send(
                {'jobId': job_id, 'uid': self.uid, 'kind': 'task_intelligence_evaluate', 'payload': {}}
            )
        except Exception:
            # The existing scheduled reconciler discovers this durable queued row.
            record_fallback(from_mode='none', to_mode='none', reason='dependency_unavailable', outcome='degraded')

    async def judge(self, judgment, shortlist, *, material_version, evaluated_at):
        if not shortlist:
            return []
        evaluation_id = _stable_id('evaluation', self.uid, self.generation, self.device_scope, material_version)
        # A return to earlier material after another projection is a new evaluation
        # execution. Queue retries against the same head keep this identity.
        self.job_id = _stable_id('task-job', self.uid, self.generation, evaluation_id, self.head_token)
        self.lease = 'lease_' + uuid.uuid4().hex
        now = int(datetime.now(timezone.utc).timestamp())
        for attempt in range(3):
            tx = CandidateTransaction(self.env, self.uid, self.generation)
            row = await tx.row('recommendation_jobs', self.job_id)
            if row:
                if row['account_generation'] != self.generation:
                    raise CandidateGenerationMismatchError('job generation mismatch')
                if row['status'] == 'failed':
                    raise EvaluationUnavailable(self.job_id, 'failed', False)
                if row['status'] == 'completed':
                    # Successful publication owns the cached head; a missing head
                    # is a storage conflict, not permission to spend on another call.
                    raise CandidateSnapshotChanged('evaluation already completed')
                if (row['status'] == 'running' and (row['lease_until'] or 0) > now) or row['next_attempt_at'] > now:
                    raise EvaluationUnavailable(self.job_id, row['status'], True)
                if int(row['attempts']) >= MAX_ATTEMPTS:
                    tx.statements.append(
                        tx.db.prepare(
                            "UPDATE cf_task_intelligence_jobs SET status='failed',lease_token=NULL,lease_until=NULL,last_error='attempts_exhausted',updated_at=? WHERE uid=? AND job_id=?"
                        ).bind(now, self.uid, self.job_id)
                    )
                    try:
                        await tx.commit()
                    except CandidateSnapshotChanged:
                        if attempt == 2:
                            raise
                        continue
                    raise EvaluationUnavailable(self.job_id, 'failed', False)
                tx.statements.append(
                    tx.db.prepare(
                        "UPDATE cf_task_intelligence_jobs SET status='running',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=? WHERE uid=? AND job_id=?"
                    ).bind(self.lease, now + LEASE_SECONDS, now, self.uid, self.job_id)
                )
            else:
                tx._insert(
                    'cf_task_intelligence_jobs',
                    {
                        'job_id': self.job_id,
                        'account_generation': self.generation,
                        'device_id': self.device_scope,
                        'request_fingerprint': _stable_id('eval-request', self.job_id),
                        'status': 'running',
                        'attempts': 1,
                        'lease_token': self.lease,
                        'lease_until': now + LEASE_SECONDS,
                        'next_attempt_at': now,
                        'input_json': encoded(
                            {'request': self.request.model_dump(mode='json'), 'material_version': material_version}
                        ),
                        'created_at': now,
                        'updated_at': now,
                    },
                )
            try:
                await tx.commit()
                break
            except CandidateSnapshotChanged:
                if attempt == 2:
                    raise
        try:
            result = await judgment.judge(shortlist)
            self.model_receipt = getattr(judgment, 'receipt', None)
            return result
        except Exception:
            await self.fail_job('judgment_failed')
            raise

    async def fail_job(self, reason):
        if not self.job_id or not self.lease:
            return
        for attempt in range(3):
            tx = CandidateTransaction(self.env, self.uid, self.generation)
            row = await tx.row('recommendation_jobs', self.job_id)
            if not row or row['status'] != 'running' or row['lease_token'] != self.lease:
                return
            now = int(datetime.now(timezone.utc).timestamp())
            status = 'queued' if row['attempts'] < MAX_ATTEMPTS else 'failed'
            tx.statements.append(
                tx.db.prepare(
                    'UPDATE cf_task_intelligence_jobs SET status=?,lease_token=NULL,lease_until=NULL,next_attempt_at=?,last_error=?,updated_at=? WHERE uid=? AND job_id=?'
                ).bind(status, now + RETRY_SECONDS if status == 'queued' else now, reason, now, self.uid, self.job_id)
            )
            try:
                await tx.commit()
                if status == 'queued':
                    await self._queue(self.job_id)
                return
            except CandidateSnapshotChanged:
                if attempt == 2:
                    raise

    async def complete_dispatched_job(self, job_id, projection):
        for attempt in range(3):
            tx = CandidateTransaction(self.env, self.uid, self.generation)
            row = await tx.row('recommendation_jobs', job_id)
            if not row or row['status'] in {'completed', 'failed'}:
                return
            if row['account_generation'] != self.generation:
                raise CandidateGenerationMismatchError('job generation mismatch')
            tx.statements.append(
                tx.db.prepare(
                    "UPDATE cf_task_intelligence_jobs SET status='completed',lease_token=NULL,lease_until=NULL,last_error=NULL,result_json=?,updated_at=? WHERE uid=? AND job_id=?"
                ).bind(
                    encoded(projection.model_dump(mode='json')),
                    int(datetime.now(timezone.utc).timestamp()),
                    self.uid,
                    job_id,
                )
            )
            try:
                await tx.commit()
                return
            except CandidateSnapshotChanged:
                if attempt == 2:
                    raise

    def _finish_job(self, tx, row, projection):
        if (
            row is None
            or row['account_generation'] != self.generation
            or row['status'] != 'running'
            or row['lease_token'] != self.lease
        ):
            raise CandidateSnapshotChanged('evaluation lease was lost')
        tx.statements.append(
            tx.db.prepare(
                "UPDATE cf_task_intelligence_jobs SET status='completed',lease_token=NULL,lease_until=NULL,last_error=NULL,result_json=?,updated_at=? WHERE uid=? AND job_id=?"
            ).bind(
                encoded(projection.model_dump(mode='json')),
                int(datetime.now(timezone.utc).timestamp()),
                self.uid,
                self.job_id,
            )
        )

    async def save_projection(self, uid, *, device_scope, projection, decisions, account_generation):
        await self.guard(uid, account_generation)
        if device_scope != self.device_scope:
            raise CandidateConflictError('projection device mismatch')
        for attempt in range(3):
            tx = CandidateTransaction(self.env, uid, account_generation)
            raw = await tx.record('recommendations', self.head_id())
            data = json.loads(raw) if raw else None
            current = parse_model(WhatMattersNowProjection, data['projection']) if data else None
            job = await tx.row('recommendation_jobs', self.job_id) if self.job_id else None
            if current and (
                current.generated_at > projection.generated_at
                or (
                    current.material_version == projection.material_version
                    and current.expires_at > projection.generated_at
                )
            ):
                if self.job_id:
                    self._finish_job(tx, job, current)
                    self._record_model(tx, projection)
                try:
                    await tx.commit()
                    return current
                except CandidateSnapshotChanged:
                    if attempt == 2:
                        raise
                    continue
            for recommendation in projection.recommendations:
                identity = recommendation.intervention_id
                existing = await tx.row('interventions', identity)
                previous = json.loads(existing['payload_json']) if existing else {}
                payload = recommendation.model_dump(mode='json') | {
                    'evaluation_id': projection.evaluation_id,
                    'attribution_chain_id': _stable_id('attr', uid, identity),
                    'account_generation': account_generation,
                    'surface': 'what_matters_now',
                    'created_at': previous.get('created_at') or projection.generated_at.isoformat(),
                }
                tx.statements.append(
                    tx.db.prepare(
                        'INSERT INTO cf_task_interventions(uid,intervention_id,account_generation,attribution_chain_id,request_fingerprint,payload_json,created_at) '
                        'VALUES (?,?,?,?,?,?,?) ON CONFLICT(uid,intervention_id) DO UPDATE SET payload_json=excluded.payload_json'
                    ).bind(
                        uid,
                        identity,
                        account_generation,
                        payload['attribution_chain_id'],
                        _stable_id('wmnow-intervention', identity),
                        encoded(payload),
                        existing['created_at'] if existing else int(projection.generated_at.timestamp()),
                    )
                )
            tx.put(
                'recommendations',
                self.head_id(),
                {'account_generation': account_generation, 'projection': projection.model_dump(mode='json')},
            )
            job_id = self.job_id or _stable_id('task-job', uid, account_generation, projection.evaluation_id)
            tx.statements.append(
                tx.db.prepare(
                    'INSERT INTO cf_task_evaluations(uid,evaluation_id,job_id,account_generation,device_id,request_fingerprint,projection_json,decisions_json,generated_at,expires_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(uid,evaluation_id) DO UPDATE SET job_id=excluded.job_id,projection_json=excluded.projection_json,decisions_json=excluded.decisions_json,generated_at=excluded.generated_at,expires_at=excluded.expires_at'
                ).bind(
                    uid,
                    projection.evaluation_id,
                    job_id,
                    account_generation,
                    device_scope,
                    projection.material_version,
                    encoded(projection.model_dump(mode='json')),
                    encoded([decision.model_dump(mode='json') for decision in decisions]),
                    int(projection.generated_at.timestamp()),
                    int(projection.expires_at.timestamp()),
                )
            )
            if self.job_id:
                self._finish_job(tx, job, projection)
                self._record_model(tx, projection)
            # Retain the original bounded per-device decision history (24).
            tx.statements.append(
                tx.db.prepare(
                    'DELETE FROM cf_task_evaluations WHERE uid=? AND account_generation=? AND device_id=? AND evaluation_id<>? '
                    'AND (expires_at<=? OR evaluation_id NOT IN (SELECT evaluation_id FROM cf_task_evaluations WHERE uid=? AND account_generation=? AND device_id=? ORDER BY generated_at DESC,evaluation_id LIMIT ?))'
                ).bind(
                    uid,
                    account_generation,
                    device_scope,
                    projection.evaluation_id,
                    int(projection.generated_at.timestamp()),
                    uid,
                    account_generation,
                    device_scope,
                    MAX_HISTORY,
                )
            )
            try:
                await tx.commit()
                return projection
            except CandidateSnapshotChanged:
                if attempt == 2:
                    raise

    def _record_model(self, tx, projection):
        receipt = self.model_receipt
        if not receipt:
            return
        tx._insert(
            'cf_task_llm_receipts',
            {
                'receipt_id': _stable_id('llm', self.uid, self.generation, self.job_id),
                'job_id': self.job_id,
                'evaluation_id': projection.evaluation_id,
                'account_generation': self.generation,
                'provider': 'workers-ai',
                'model_version': receipt['model'],
                'request_fingerprint': receipt['request_hash'],
                'response_fingerprint': receipt['response_hash'],
                'status': 'completed',
                'created_at': projection.generated_at,
            },
        )
        now = int(datetime.now(timezone.utc).timestamp())
        inputs, outputs = receipt['input_tokens'], receipt['output_tokens']
        tx.statements.append(
            tx.db.prepare(
                "INSERT INTO cf_llm_usage_daily(uid,usage_day,usage_kind,feature,model,account,input_tokens,output_tokens,total_tokens,call_count,updated_at) "
                "VALUES (?,?,'feature','what_matters_now',?,'cloudflare',?,?,?,1,?) "
                "ON CONFLICT(uid,usage_day,usage_kind,feature,model,account) DO UPDATE SET input_tokens=input_tokens+excluded.input_tokens,output_tokens=output_tokens+excluded.output_tokens,total_tokens=total_tokens+excluded.total_tokens,call_count=call_count+1,updated_at=excluded.updated_at"
            ).bind(
                self.uid,
                datetime.fromtimestamp(now, timezone.utc).date().isoformat(),
                receipt['model'],
                inputs,
                outputs,
                inputs + outputs,
                now,
            )
        )

    def record_intervention_metric(self, subject_kind):
        if subject_kind not in {'candidate', 'task', 'workstream', 'artifact', 'decision'}:
            raise ValueError('invalid attribution metric label')
        print(
            encoded(
                {
                    'event': 'task_intelligence_attribution',
                    'kind': 'intervention',
                    'subject_kind': subject_kind,
                    'code': 'what_matters_now',
                }
            )
        )
