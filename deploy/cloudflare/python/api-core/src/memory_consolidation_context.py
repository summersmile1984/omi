"""Build original consolidation context from Workers AI/Vectorize and owned D1.

Vectorize supplies scored IDs only. The existing canonical hydration owner
validates revision, visibility and tenant identity and repairs stale mappings.
Complete source text is searched in bounded overlapping embedding windows;
no user text is silently dropped to fit the public search endpoint's limit.
"""

from datetime import datetime, timezone

from fallback import record_fallback
from memory_apply_intake import encoded, load_memory_control
from memory_apply_item import read_item
from memory_consolidation_apply import MAX_CONSOLIDATION_BATCH_ITEMS, _hydrate_context
from memory_kernel_consolidation import (
    ConsolidationApplySkipped,
    ConsolidationCandidate,
    ConsolidationContext,
    RejectedMemoryFeedback,
    REJECTED_MEMORY_FEEDBACK_MAX_AGE,
    REJECTED_MEMORY_FEEDBACK_SCAN_LIMIT,
    bound_rejected_memory_examples,
    _has_restricted_sensitivity,
    _is_prompt_eligible_rejection,
)
from memory_vector_hydration import hydrate_memory_vectors
from memory_vector_readiness import ensure_memory_index_ready, assert_memory_index_snapshot
from vector_search import embed_query, query_vector_ids

DEFAULT_CANDIDATES_PER_ITEM = 8
EMBEDDING_WINDOW_CHARS = 3_500
EMBEDDING_WINDOW_OVERLAP = 350
MAX_SOURCE_CHARS = 50_000


def embedding_windows(content):
    if len(content) > MAX_SOURCE_CHARS:
        raise ConsolidationApplySkipped('memory_consolidation_source_too_large')
    stride = EMBEDDING_WINDOW_CHARS - EMBEDDING_WINDOW_OVERLAP
    for start in range(0, len(content), stride):
        value = content[start : start + EMBEDDING_WINDOW_CHARS].strip()
        if value:
            yield value
        if start + EMBEDDING_WINDOW_CHARS >= len(content):
            break


async def rejected_feedback(env, uid, *, now):
    cutoff = now - REJECTED_MEMORY_FEEDBACK_MAX_AGE
    rows = (
        await env.APP_DB.prepare(
            "SELECT * FROM cf_memories WHERE uid = ? AND status IN ('active', 'hidden') "
            "AND source_state = 'active' AND user_review = 0 AND updated_at >= ? "
            'AND deleted_at IS NULL AND invalid_at IS NULL AND superseded_by IS NULL '
            'ORDER BY updated_at DESC, id LIMIT ?'
        )
        .bind(uid, int(cutoff.timestamp()), REJECTED_MEMORY_FEEDBACK_SCAN_LIMIT)
        .all()
    )['results']
    examples = []
    for row in rows:
        try:
            item = read_item(row)
        except (ValueError, TypeError, KeyError):
            continue
        if not _is_prompt_eligible_rejection(item, uid=uid, cutoff=cutoff):
            continue
        content = bound_rejected_memory_examples([item.content])
        if content:
            examples.append(RejectedMemoryFeedback(item.memory_id, content[0], item.updated_at))
    by_content = {}
    for example in examples:
        by_content.setdefault(example.content, example)
    return [by_content[content] for content in bound_rejected_memory_examples([item.content for item in examples])]


async def gather_consolidation_context(env, uid, memory_ids, *, candidate_limit=DEFAULT_CANDIDATES_PER_ITEM, now=None):
    ids = list(memory_ids)
    if (
        not isinstance(uid, str)
        or not uid.strip()
        or not 1 <= len(ids) <= MAX_CONSOLIDATION_BATCH_ITEMS
        or any(not isinstance(value, str) or not value.strip() for value in ids)
        or len(set(ids)) != len(ids)
        or type(candidate_limit) is not int
        or not 1 <= candidate_limit <= 20
    ):
        raise ValueError('invalid consolidation context request')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('consolidation time must be aware')
    _, control = await load_memory_control(env, uid)
    rows = (
        await env.APP_DB.prepare('SELECT * FROM cf_memories WHERE uid = ? AND id IN (SELECT value FROM json_each(?))')
        .bind(uid, encoded(ids))
        .all()
    )['results']
    items = {row['id']: read_item(row) for row in rows}
    if set(items) != set(ids):
        raise ConsolidationApplySkipped('memory_consolidation_source_changed')
    context = ConsolidationContext(uid=uid, pending_items=[items[key] for key in ids])
    _, _, context = await _hydrate_context(env, context, control.account_generation)
    projection_sequence = await ensure_memory_index_ready(env, uid, ids)
    # Reuse the original feedback limits and policy. A database outage cannot
    # silently remove an owner's negative feedback from this migration path.
    context.owner_rejected_examples = [
        value for value in await rejected_feedback(env, uid, now=now) if value.memory_id not in items
    ]
    for anchor in context.pending_items:
        candidates = []
        context.candidates_by_anchor[anchor.memory_id] = candidates
        content = (anchor.content or '').strip()
        if not content or _has_restricted_sensitivity(anchor.sensitivity_labels):
            continue
        matches = {}
        try:
            for window in embedding_windows(content):
                _, disclosure_control = await load_memory_control(env, uid)
                if disclosure_control != control:
                    raise ConsolidationApplySkipped('memory_consolidation_authority_changed')
                vector = await embed_query(env, window)
                for vector_id, score in await query_vector_ids(
                    env, 'MEMORY_VECTORS', uid, vector, top_k=candidate_limit + 1
                ):
                    matches[vector_id] = max(matches.get(vector_id, score), score)
            # Hydration has an explicit 100-ID budget. Ranking all windows
            # before this bound lets a match near the end of a long source win.
            ranked = sorted(matches.items(), key=lambda pair: pair[1], reverse=True)[:100]
            hydrated = await hydrate_memory_vectors(env, uid, ranked)
        except ConsolidationApplySkipped:
            raise
        except Exception as error:
            record_fallback(
                component='other',
                from_mode='vectorize',
                to_mode='none',
                reason='dependency_unavailable',
                outcome='exhausted',
            )
            raise ConsolidationApplySkipped('memory_consolidation_candidates_unavailable') from error
        for row in hydrated.rows:
            item = read_item(row)
            if item.memory_id == anchor.memory_id:
                continue
            candidates.append(
                ConsolidationCandidate(
                    anchor_memory_id=anchor.memory_id,
                    memory_id=item.memory_id,
                    content=item.content or '',
                    score=hydrated.scores[item.memory_id],
                    tier=item.tier.value,
                    captured_at=item.captured_at.isoformat(),
                    sensitivity_labels=tuple(item.sensitivity_labels),
                    user_rejected=(item.promotion or {}).get('user_review') is False,
                )
            )
            if len(candidates) >= candidate_limit:
                break
    await assert_memory_index_snapshot(env, uid, ids, projection_sequence)
    _, after = await load_memory_control(env, uid)
    if after != control:
        raise ConsolidationApplySkipped('memory_consolidation_authority_changed')
    _, _, current = await _hydrate_context(env, context, control.account_generation)
    return current
