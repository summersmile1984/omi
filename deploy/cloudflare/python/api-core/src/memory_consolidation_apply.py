"""D1 transaction adapter for a complete, source-fenced upstream L2 decision batch.

The maintenance planner supplies hydrated context and its single model response.
This owner validates the whole partition before any write; it never classifies
content or supplies a replacement decision when the model fails.
"""

from dataclasses import replace
from datetime import datetime, timezone

from memory_apply_item import read_item
from memory_apply_intake import (
    MODEL_COLUMNS,
    _insert_rows,
    _stored_item,
    append_journal_records,
    control_statement,
    encoded,
    load_memory_control,
)
from memory_consolidation_leases import settlement_statements, verify_consolidation_leases
from memory_consolidation_policy import consolidation_route_patch, required_processing_patch
from memory_kernel_apply import ApplyStatus, apply_long_term_patch_transaction
from memory_kernel_consolidation import (
    ConsolidationApplySkipped,
    ConsolidationAgentBatch,
    ConsolidationCandidate,
    ConsolidationContext,
    _processed_from_consolidation_decision,
    _validate_agent_batch,
    _terminal_review_decision,
    MAX_CONSOLIDATION_FAILURE_ATTEMPTS,
    is_pending_required_processing,
    _is_prompt_eligible_rejection,
    bound_rejected_memory_examples,
    REJECTED_MEMORY_FEEDBACK_MAX_AGE,
)
from memory_kernel_duplicate_admission import MemoryIdentity, validate_duplicate_creates
from memory_kernel_item import MemoryItem
from memory_kernel_review import build_memory_review_conflict
from vector_search import publish_vector_projection

MAX_CONSOLIDATION_BATCH_ITEMS = 20


def _review_record(uid, item, commit_id):
    promotion = item.promotion or {}
    if item.status.value != 'active' or promotion.get('route') != 'review':
        return None
    conflict = promotion.get('target_memory_id')
    review = build_memory_review_conflict(
        fact={'id': item.memory_id, 'content': item.content, 'veracity': 0.4, 'importance': 0.5},
        conflict_with=[conflict] if isinstance(conflict, str) and conflict else [],
        authority='canonical_memory',
        source_commit_id=commit_id,
        source_item_revision=item.item_revision,
        source_content_hash=item.content_hash,
        source_short_term_id=item.memory_id,
        impact=0.5,
        now=item.updated_at,
    )
    return {
        'uid': uid,
        **{
            key + '_json': encoded(review[key])
            for key in (
                'candidate',
                'conflict_with',
                'referenced_memory_ids',
                'permitted_uses',
            )
        },
        **{key: int(review[key].timestamp()) for key in ('created_at', 'updated_at', 'expires_at')},
        **{
            key: review[key]
            for key in (
                'review_id',
                'fact_id',
                'veracity',
                'impact',
                'status',
                'authority',
                'source_commit_id',
                'source_item_revision',
                'source_content_hash',
                'source_short_term_id',
            )
        },
    }


async def _hydrate_context(env, context, generation):
    identifiers = context.hydrated_memory_ids
    result = (
        await env.APP_DB.prepare(
            'SELECT * FROM cf_memories WHERE uid = ? AND id IN (SELECT value FROM json_each(?)) '
            "AND status IN ('active', 'hidden') AND source_state = 'active' AND deleted_at IS NULL AND invalid_at IS NULL "
            'AND superseded_by IS NULL AND account_generation = ?'
        )
        .bind(context.uid, encoded(sorted(identifiers)), generation)
        .all()
    )
    rows = {row['id']: row for row in result['results']}
    if set(rows) != identifiers:
        raise ConsolidationApplySkipped('memory_consolidation_source_changed')
    items = {key: read_item(row) for key, row in rows.items()}
    for expected in context.pending_items:
        actual = items[expected.memory_id]
        if expected.uid != context.uid or actual.model_dump(mode='json') != expected.model_dump(mode='json'):
            raise ConsolidationApplySkipped('memory_consolidation_source_changed')
        if (
            actual.status.value != 'active'
            or actual.tier.value != 'short_term'
            or (actual.processing_state.value != 'processed' and not is_pending_required_processing(actual))
        ):
            raise ConsolidationApplySkipped('memory_consolidation_source_not_pending')
    candidates = {}
    for anchor, values in context.candidates_by_anchor.items():
        if anchor not in {item.memory_id for item in context.pending_items}:
            raise ConsolidationApplySkipped('memory_consolidation_unknown_anchor')
        candidates[anchor] = []
        for previous in values:
            item = items[previous.memory_id]
            if item.status.value != 'active':
                raise ConsolidationApplySkipped('memory_consolidation_candidate_changed')
            current = ConsolidationCandidate(
                anchor_memory_id=anchor,
                memory_id=item.memory_id,
                content=item.content or '',
                score=previous.score,
                tier=item.tier.value,
                captured_at=item.captured_at.isoformat(),
                sensitivity_labels=tuple(item.sensitivity_labels),
                user_rejected=(item.promotion or {}).get('user_review') is False,
            )
            if current != previous:
                raise ConsolidationApplySkipped('memory_consolidation_candidate_changed')
            candidates[anchor].append(current)
    # Negative examples are model input too. A concurrent edit/owner vote must
    # cause replanning, even when the model did not name that example as a target.
    for feedback in context.owner_rejected_examples:
        item = items[feedback.memory_id]
        if (
            not _is_prompt_eligible_rejection(
                item, uid=context.uid, cutoff=datetime.now(timezone.utc) - REJECTED_MEMORY_FEEDBACK_MAX_AGE
            )
            or bound_rejected_memory_examples([item.content]) != (feedback.content,)
            or item.updated_at != feedback.updated_at
        ):
            raise ConsolidationApplySkipped('memory_consolidation_feedback_changed')
    return (
        rows,
        items,
        replace(
            context,
            pending_items=[items[item.memory_id] for item in context.pending_items],
            candidates_by_anchor=candidates,
        ),
    )


async def apply_consolidation_batch(
    env,
    context: ConsolidationContext,
    batch: ConsolidationAgentBatch,
    *,
    run_id: str,
    now,
    leases=(),
    terminal_status=None
):
    """Apply normalization, routes, supersession, graphs and reviews atomically.

    A stale or replayed context cannot produce another route. The maintenance
    owner must reload pending work after an uncertain delivery, as for any
    account-head conflict. Recurrence needs its workflow handoff before this
    adapter can acknowledge such a batch; no signal is silently discarded.
    """
    if now.tzinfo is None or now.utcoffset() is None or not run_id.strip():
        raise ValueError('consolidation requires an aware timestamp and run identity')
    if not 1 <= len(context.pending_items) <= MAX_CONSOLIDATION_BATCH_ITEMS or len(
        {item.memory_id for item in context.pending_items}
    ) != len(context.pending_items):
        raise ValueError('invalid consolidation batch size or identity')
    if terminal_status is not None:
        if (
            terminal_status not in {'terminal_review', 'quarantined'}
            or len(leases) != 1
            or len(context.pending_items) != 1
            or leases[0].state.attempt_count != MAX_CONSOLIDATION_FAILURE_ATTEMPTS
            or batch.recurrence_signals
            or batch.decisions != [_terminal_review_decision(context.pending_items[0])]
        ):
            raise ValueError('invalid consolidation terminal settlement')
    batch = ConsolidationAgentBatch.model_validate(batch.model_dump())
    prior, initial_control = await load_memory_control(env, context.uid)
    rows, items, current_context = await _hydrate_context(env, context, initial_control.account_generation)
    await verify_consolidation_leases(env, leases, current_context.pending_items, initial_control)
    error = _validate_agent_batch(current_context, batch)
    if error is None:
        error = validate_duplicate_creates(
            current_context, batch, {key: MemoryIdentity.from_item(item) for key, item in items.items()}
        )
    if error is not None:
        raise ConsolidationApplySkipped(error)
    if batch.recurrence_signals:
        raise ConsolidationApplySkipped('memory_recurrence_handoff_unavailable')

    control = initial_control
    steps = []

    def plan(source, operation, patch):
        nonlocal control
        payload = {
            **patch,
            'existing_item': source,
            'evidence': source.evidence,
            'superseded_items': [items[key] for key in patch.get('supersedes', [])],
        }
        result = apply_long_term_patch_transaction(control_state=control, operation=operation, patch_payload=payload)
        if result.status != ApplyStatus.committed:
            raise ConsolidationApplySkipped('memory_consolidation_apply_not_admitted:' + result.status.value)
        steps.append((control, result))
        for raw in result.memory_items:
            item = MemoryItem.model_validate(raw.model_dump())
            items[item.memory_id] = item
        control = result.control_state

    for decision in sorted(batch.decisions, key=lambda value: value.route != 'promote'):
        source = items[decision.source_memory_id]
        if is_pending_required_processing(source):
            processed = _processed_from_consolidation_decision(source, decision)
            operation, patch = required_processing_patch(source, processed, control, now)
            plan(source, operation, patch)
            source = items[source.memory_id]
        operation, patch = consolidation_route_patch(
            source, decision, control, run_id, now, quarantine=terminal_status == 'quarantined'
        )
        plan(source, operation, patch)

    db, uid = env.APP_DB, context.uid
    writable_ids = {item.memory_id for _, result in steps for item in result.memory_items}
    expected = [
        {
            key: row[key]
            for key in (
                'id',
                'item_revision',
                'version',
                'canonical_metadata_json',
                'capture_device_ids_json',
                'primary_capture_device',
                'status',
                'is_locked',
            )
        }
        for row in rows.values()
    ]
    statements = [
        db.prepare(
            'INSERT INTO cf_memory_apply_guard (uid, expected_control_json, account_generation, '
            'new_ids_json, operation_ids_json, expected_items_json, consolidation_claims_json, observed_items_json) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)'
        ).bind(
            uid,
            prior,
            initial_control.account_generation,
            '[]',
            encoded([result.operation.operation_id for _, result in steps]),
            encoded([row for row in expected if row['id'] in writable_ids]),
            encoded([lease.claim() for lease in leases]),
            encoded([row for row in expected if row['id'] not in writable_ids]),
        )
    ]
    columns = sorted((set(MODEL_COLUMNS.values()) | {'canonical_metadata_json'}) - {'uid', 'id'})
    changed_ids = set()
    for previous_control, result in steps:
        for raw in result.memory_items:
            item = MemoryItem.model_validate(raw.model_dump())
            stored = _stored_item(rows[item.memory_id], item)
            statements.append(
                db.prepare(
                    'UPDATE cf_memories SET '
                    + ', '.join(column + ' = ?' for column in columns)
                    + ' WHERE uid = ? AND id = ?'
                ).bind(*(stored[column] for column in columns), uid, item.memory_id)
            )
            rows[item.memory_id] = stored
            changed_ids.add(item.memory_id)
            review = _review_record(uid, item, result.control_state.head_commit_id)
            if review:
                statements.extend(_insert_rows(db, 'cf_memory_review_queue', [review]))
        statements.extend(
            _insert_rows(
                db,
                'cf_memory_graph_assertions',
                [
                    {
                        'uid': uid,
                        'memory_id': assertion.memory_id,
                        'item_revision': assertion.item_revision,
                        'account_generation': initial_control.account_generation,
                        'assertion_json': assertion.model_dump_json(),
                    }
                    for assertion in result.graph_assertions
                ],
            )
        )
        records = {table: [] for table in ('cf_memory_operations', 'cf_memory_commits', 'cf_memory_outbox')}
        append_journal_records(records, uid, result, previous_control, int(now.timestamp()))
        for table, values in records.items():
            statements.extend(_insert_rows(db, table, values))
        statements.append(control_statement(db, uid, result.control_state))
    statements.extend(settlement_statements(db, leases, terminal_status=terminal_status, now=now))
    statements.append(db.prepare('DELETE FROM cf_memory_apply_guard WHERE uid = ?').bind(uid))
    await db.batch(statements)
    for memory_id in sorted(changed_ids):
        await publish_vector_projection(env, uid=uid, source_kind='memory', source_id=memory_id)
    # Return the stored representation: physical timestamps are integer seconds
    # and arbitrary promotion metadata crosses the ordinary JSON boundary.
    return {key: read_item(rows[key]) for key in sorted(changed_ids)}
