"""Bounded, durable ANN visibility admission before consolidation retrieval.

The immutable publication ID is an opaque indexed metadata key, never content
or authorization. D1 still owns tenant, revision, eligibility and completeness.
Query proofs survive a continuation; missing projections use the existing
outbox/reconciler. A dependency wait must not consume a model failure attempt.
"""

import asyncio
import math

from memory_apply_intake import encoded
from memory_kernel_consolidation import ConsolidationApplySkipped
from vector_search import VECTOR_MODEL, _to_python, publish_vector_projection, vector_namespace

READINESS_BATCH = 20
READINESS_TIMEOUT_SECONDS = 15


class ConsolidationIndexPending(ConsolidationApplySkipped):
    """Resume later without spending an inference attempt or changing a route."""

    retry_after_seconds = 5


_SCOPE = "m.uid = ? AND m.operation = 'upsert' " 'AND m.id NOT IN (SELECT value FROM json_each(?))'
_COMPLETE = 'p.uid = m.uid AND p.source_id = m.id ' 'AND p.source_version = m.item_revision AND p.model = ?'
_UNFENCED = (
    'NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) '
    'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?)'
)


async def _snapshot(env, uid, ids, model):
    return (
        await env.APP_DB.prepare(
            'SELECT (SELECT projection_sequence FROM cf_memory_apply_control WHERE uid = ?) AS sequence, '
            '(EXISTS (SELECT 1 FROM cf_memory_projection_sources m '
            f'WHERE {_SCOPE} AND NOT EXISTS (SELECT 1 FROM cf_memory_vector_publications p '
            f'WHERE {_COMPLETE} AND p.query_ready = 1))) AS pending'
        )
        .bind(uid, uid, encoded(ids), model)
        .first()
    )


async def assert_memory_index_snapshot(env, uid, ids, sequence):
    model = getattr(env, 'WORKERS_AI_VECTOR_MODEL', VECTOR_MODEL)
    current = await _snapshot(env, uid, ids, model)
    if current['sequence'] != sequence or current['pending']:
        raise ConsolidationIndexPending('memory_consolidation_index_changed')


async def ensure_memory_index_ready(env, uid, ids):
    model = getattr(env, 'WORKERS_AI_VECTOR_MODEL', VECTOR_MODEL)
    before = await _snapshot(env, uid, ids, model)
    if before['sequence'] is None:
        raise ConsolidationIndexPending('memory_consolidation_control_not_materialized')
    if not before['pending']:
        return before['sequence']
    # Missing, partial and legacy publications need the same repair as an old
    # model/revision. Re-read intent in this one SQL statement; preserve an
    # existing retry's attempts and backoff when the desired version is equal.
    queued = (
        await env.APP_DB.prepare(
            'INSERT INTO cf_vector_projection_outbox '
            '(uid, source_kind, source_id, desired_version, operation, attempts, '
            'next_attempt_at, last_error, created_at, updated_at) '
            "SELECT m.uid, 'memory', m.id, m.item_revision, 'upsert', 0, unixepoch(), NULL, unixepoch(), unixepoch() "
            f'FROM cf_memory_projection_sources m WHERE {_SCOPE} AND {_UNFENCED} '
            f'AND NOT EXISTS (SELECT 1 FROM cf_memory_vector_publications p WHERE {_COMPLETE}) '
            'ORDER BY m.item_revision, m.id LIMIT ? '
            'ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET '
            'desired_version = excluded.desired_version, operation = excluded.operation, attempts = 0, '
            'next_attempt_at = excluded.next_attempt_at, last_error = NULL, updated_at = excluded.updated_at '
            'WHERE excluded.desired_version > cf_vector_projection_outbox.desired_version '
            "OR cf_vector_projection_outbox.operation != 'upsert' RETURNING source_id"
        )
        .bind(uid, encoded(ids), uid, uid, model, READINESS_BATCH)
        .all()
    )
    for row in queued['results']:
        await publish_vector_projection(env, uid=uid, source_kind='memory', source_id=row['source_id'])
    rows = (
        await env.APP_DB.prepare(
            'SELECT a.vector_id FROM cf_memory_projection_sources m '
            f'JOIN cf_memory_vector_publications p ON {_COMPLETE} '
            'JOIN cf_memory_vector_artifacts a ON a.uid = p.uid AND a.source_id = p.source_id '
            'AND a.attempt_id = p.attempt_id '
            f'WHERE {_SCOPE} AND a.query_ready = 0 AND a.retired = 0 AND a.writer_done = 1 '
            'ORDER BY a.vector_id LIMIT ?'
        )
        .bind(model, uid, encoded(ids), READINESS_BATCH)
        .all()
    )['results']
    for row in rows:
        vector_id = row['vector_id']
        try:
            result = _to_python(
                await asyncio.wait_for(
                    env.MEMORY_VECTORS.queryById(
                        vector_id,
                        {
                            'namespace': vector_namespace(uid),
                            'topK': 1,
                            'filter': {'publication_id': vector_id},
                            'returnValues': False,
                            'returnMetadata': 'none',
                        },
                    ),
                    timeout=READINESS_TIMEOUT_SECONDS,
                )
            )
        except Exception as error:
            raise ConsolidationIndexPending('memory_consolidation_index_unavailable') from error
        matches = result.get('matches') if isinstance(result, dict) else None
        if not isinstance(matches, list) or len(matches) != 1:
            continue
        match = matches[0]
        score = match.get('score') if isinstance(match, dict) else None
        if (
            not isinstance(match, dict)
            or match.get('id') != vector_id
            or type(score) not in (int, float)
            or not math.isfinite(score)
        ):
            continue
        # The query may overlap a source edit or deletion. Only the exact live
        # mapping can gain a proof; obsolete artifacts keep their cleanup owner.
        await env.APP_DB.prepare(
            'UPDATE cf_memory_vector_artifacts AS a SET query_ready = 1 '
            f'WHERE a.uid = ? AND a.vector_id = ? AND {_UNFENCED} '
            'AND a.retired = 0 AND a.writer_done = 1 '
            'AND EXISTS (SELECT 1 FROM cf_memory_vector_publications p '
            'JOIN cf_memory_projection_sources m ON m.uid = p.uid AND m.id = p.source_id '
            "AND m.operation = 'upsert' AND m.item_revision = p.source_version "
            'WHERE p.uid = a.uid AND p.source_id = a.source_id AND p.attempt_id = a.attempt_id '
            'AND p.model = ?)'
        ).bind(uid, vector_id, uid, uid, model).run()
    await assert_memory_index_snapshot(env, uid, ids, before['sequence'])
    return before['sequence']
