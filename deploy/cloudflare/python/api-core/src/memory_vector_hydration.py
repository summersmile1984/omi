"""One canonical snapshot for memory-vector content, freshness and admission.

Repair uses the existing artifact journal and canonical projection outbox. It
does not create a second external deletion owner or trust provider metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import json

from fallback import record_fallback
from vector_search import VECTOR_MODEL, publish_vector_projection


@dataclass
class MemoryHydration:
    rows: list[dict[str, object]] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    decisions: dict[str, str] = field(default_factory=dict)
    rejected: dict[str, int] = field(
        default_factory=lambda: {
            'missing': 0,
            'stale_projection': 0,
            'stale_vector': 0,
            'access_denied': 0,
        }
    )
    repairs: list[dict[str, object]] = field(default_factory=list)
    records: list[dict[str, object]] = field(default_factory=list)
    reads: int = 0
    authoritative_count: int = 0


_UNFENCED = (
    'NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) '
    'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones '
    'WHERE uid = ? AND expires_at > unixepoch())'
)

_CURRENT = (
    "EXISTS (SELECT 1 FROM cf_memory_projection_sources v "
    "WHERE v.uid = s.uid AND v.id = s.source_id AND v.operation = 'upsert' "
    "AND v.item_revision = s.source_version AND s.model = ?) "
    "AND EXISTS (SELECT 1 FROM cf_memory_vector_artifacts a "
    "WHERE a.uid = s.uid AND a.vector_id = s.vector_id "
    "AND a.retired = 0 AND a.source_version = s.source_version AND a.model = s.model)"
)


async def _repair(env: object, uid: str, report: MemoryHydration, model: str) -> None:
    if not report.repairs:
        return
    statements = []
    sources = set()
    for candidate in report.repairs:
        vector_id = candidate['vector_id']
        sources.add(candidate['memory_id'])
        # Adopt a legacy mapping before revoking it. Its old writer may still
        # exist, so only the bounded Jobs artifact owner can later delete it.
        statements.append(
            env.APP_DB.prepare(
                'INSERT INTO cf_memory_vector_artifacts '
                '(vector_id, uid, source_id, attempt_id, sub_id, source_version, model, '
                'writer_until, writer_done, observed_present) '
                "SELECT ?, ?, ?, 'repair:' || ?, '', ?, ?, unixepoch() + 900, 0, 1 "
                'WHERE NOT EXISTS (SELECT 1 FROM cf_memory_vector_artifacts a WHERE a.vector_id = ?) '
                f'AND {_UNFENCED}'
            ).bind(
                vector_id,
                uid,
                candidate['memory_id'],
                vector_id,
                candidate['observed_item_revision'],
                candidate['observed_model'],
                vector_id,
                uid,
                uid,
            )
        )
        statements.append(
            env.APP_DB.prepare(
                'DELETE FROM cf_vector_projection_state AS s '
                "WHERE s.uid = ? AND s.projection_kind = 'memory' AND s.vector_id = ? "
                f'AND NOT ({_CURRENT})'
            ).bind(uid, vector_id, model)
        )
    for source_id in sorted(sources):
        # Re-read canonical intent in the write transaction. A memory edited or
        # recreated since hydration must enqueue its new revision, not the old
        # observed repair. Healthy current mappings need only artifact cleanup.
        statements.append(
            env.APP_DB.prepare(
                'WITH desired AS ('
                'SELECT uid, id, item_revision AS version, operation '
                'FROM cf_memory_projection_sources WHERE uid = ? AND id = ? '
                'UNION ALL '
                "SELECT ?, ?, COALESCE(MAX(source_version), 0) + 1, 'delete' "
                'FROM cf_memory_vector_artifacts WHERE uid = ? AND source_id = ? '
                'HAVING NOT EXISTS (SELECT 1 FROM cf_memories WHERE uid = ? AND id = ?)'
                ') INSERT INTO cf_vector_projection_outbox '
                '(uid, source_kind, source_id, desired_version, operation, attempts, '
                'next_attempt_at, last_error, created_at, updated_at) '
                "SELECT uid, 'memory', id, version, operation, 0, unixepoch(), NULL, unixepoch(), unixepoch() "
                f'FROM desired WHERE {_UNFENCED} '
                "AND (operation = 'delete' OR NOT EXISTS (SELECT 1 FROM cf_vector_projection_state s "
                "WHERE s.uid = desired.uid AND s.source_id = desired.id AND s.projection_kind = 'memory' "
                f'AND {_CURRENT})) '
                'ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET '
                'desired_version = excluded.desired_version, operation = excluded.operation, attempts = 0, '
                'next_attempt_at = excluded.next_attempt_at, last_error = NULL, updated_at = excluded.updated_at '
                'WHERE excluded.desired_version > cf_vector_projection_outbox.desired_version '
                'OR (excluded.desired_version = cf_vector_projection_outbox.desired_version '
                'AND excluded.operation != cf_vector_projection_outbox.operation)'
            ).bind(uid, source_id, uid, source_id, uid, source_id, uid, source_id, uid, uid, model)
        )
    await env.APP_DB.batch(statements)
    ready = (
        await env.APP_DB.prepare(
            "SELECT source_id FROM cf_vector_projection_outbox WHERE uid = ? AND source_kind = 'memory' "
            'AND source_id IN (SELECT value FROM json_each(?)) AND next_attempt_at <= unixepoch()'
        )
        .bind(uid, json.dumps(sorted(sources)))
        .all()
    )
    await asyncio.gather(
        *(
            publish_vector_projection(env, uid=uid, source_kind='memory', source_id=row['source_id'])
            for row in ready.get('results', [])
        )
    )
    ids = [candidate['vector_id'] for candidate in report.repairs]
    result = (
        await env.APP_DB.prepare(
            'SELECT a.vector_id AS record_id, a.vector_id, a.source_id AS memory_id, '
            'a.source_version, a.retired, a.writer_done, a.writer_until, a.delete_mutation '
            'FROM cf_memory_vector_artifacts a WHERE a.uid = ? '
            'AND a.vector_id IN (SELECT value FROM json_each(?)) '
            "AND NOT EXISTS (SELECT 1 FROM cf_vector_projection_state s WHERE s.uid = a.uid "
            "AND s.projection_kind = 'memory' AND s.vector_id = a.vector_id)"
        )
        .bind(uid, json.dumps(ids))
        .all()
    )
    report.records = [
        {**row, 'storage': 'cf_memory_vector_artifacts', 'status': 'pending'} for row in result.get('results', [])
    ]


async def hydrate_memory_vectors(
    env: object,
    uid: str,
    matches: list[tuple[str, float]],
) -> MemoryHydration:
    report = MemoryHydration()
    if not matches:
        return report
    if len(matches) > 100:
        raise ValueError('memory candidate budget exceeded')
    model = getattr(env, 'WORKERS_AI_VECTOR_MODEL', VECTOR_MODEL)
    ids = list(dict.fromkeys(vector_id for vector_id, _ in matches))
    result = (
        await env.APP_DB.prepare(
            'SELECT m.*, h.value AS candidate_vector_id, '
            'COALESCE(s.source_id, a.source_id) AS candidate_memory_id, '
            's.vector_id AS current_vector_id, s.source_version AS projection_revision, s.model AS projection_model, '
            'a.source_version AS artifact_revision, a.retired AS artifact_retired, a.model AS artifact_model, '
            'v.operation AS projection_operation, '
            f'NOT ({_UNFENCED}) AS account_fenced '
            'FROM json_each(?) h '
            "LEFT JOIN cf_vector_projection_state s ON s.uid = ? AND s.projection_kind = 'memory' "
            'AND s.vector_id = h.value '
            'LEFT JOIN cf_memory_vector_artifacts a ON a.uid = ? AND a.vector_id = h.value '
            'LEFT JOIN cf_memories m ON m.uid = ? AND m.id = COALESCE(s.source_id, a.source_id) '
            'LEFT JOIN cf_memory_projection_sources v ON v.uid = m.uid AND v.id = m.id'
        )
        .bind(uid, uid, json.dumps(ids), uid, uid, uid)
        .all()
    )
    values = result.get('results', [])
    if len(values) != len(ids):
        raise RuntimeError('incomplete memory hydration snapshot')
    by_vector = {row['candidate_vector_id']: row for row in values}
    report.reads = len(values)
    report.authoritative_count = len({row['id'] for row in values if row.get('id')})
    for vector_id, score in sorted(matches, key=lambda match: match[1], reverse=True):
        row = by_vector[vector_id]
        memory_id = row.get('candidate_memory_id')
        revision = row.get('item_revision')
        observed = row.get('projection_revision')
        if observed is None:
            observed = row.get('artifact_revision')
        if row.get('account_fenced'):
            decision, reason = 'access_denied', None
        elif not row.get('id'):
            decision, reason = 'missing', 'missing_authoritative_item'
        elif row.get('projection_operation') != 'upsert' or not str(row.get('content') or '').strip():
            decision, reason = 'access_denied', None
        elif observed != revision or (row.get('current_vector_id') and row.get('projection_model') != model):
            decision, reason = 'stale_projection', 'stale_projection_commit'
        elif (
            not row.get('current_vector_id')
            or row.get('artifact_revision') is None
            or row.get('artifact_retired') == 1
            or (
                row.get('artifact_revision') is not None
                and (row.get('artifact_revision') != revision or row.get('artifact_model') != model)
            )
        ):
            decision, reason = 'stale_vector', 'missing_vector_freshness_metadata'
        else:
            if memory_id not in report.scores:
                report.rows.append(row)
                report.scores[str(memory_id)] = score
                report.versions[str(memory_id)] = str(revision)
                report.decisions[str(memory_id)] = 'allowed'
            continue
        report.rejected[decision] += 1
        if memory_id:
            if str(memory_id) not in report.scores:
                report.decisions[str(memory_id)] = 'missing_authoritative_item' if decision == 'missing' else decision
            if reason:
                report.repairs.append(
                    {
                        'vector_id': vector_id,
                        'memory_id': memory_id,
                        'decision': 'missing_authoritative_item' if decision == 'missing' else decision,
                        'reason': reason,
                        'observed_projection_commit_id': None if observed is None else str(observed),
                        'required_projection_commit_id': None if revision is None else str(revision),
                        'observed_item_revision': observed,
                        'authoritative_item_revision': revision,
                        'observed_model': row.get('projection_model') or row.get('artifact_model'),
                    }
                )
    if report.repairs or report.rejected['missing']:
        record_fallback(from_mode='none', to_mode='none', reason='malformed_doc', outcome='degraded')
    await _repair(env, uid, report, model)
    return report
