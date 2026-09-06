"""D1 conversation/fact snapshots for the original read-only Developer RAG prompt."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from developer_ask_contract import _ask_context_from_conversations
from developer_ask_prompt import PROMPT_MEMORY_LIMIT, _render_legacy_prompt_context
from fallback import record_fallback
from vector_search import embed_query, hydrate_candidate_ids, query_vector_ids

CONVERSATION_FIELDS = (
    "id, json_extract(structured_json, '$.title') AS title, "
    "json_extract(structured_json, '$.overview') AS overview, transcript_segments_json, created_at, updated_at"
)
CONVERSATION_VISIBLE = "is_locked = 0 AND discarded = 0 AND status = 'completed'"
FACT_FIELDS = 'id, content, created_at, item_revision, is_baseline, manually_added'
FACT_VISIBLE = (
    "deleted_at IS NULL AND invalid_at IS NULL AND is_locked = 0 AND user_review IS NOT 0 "
    "AND memory_tier IN ('short_term', 'long_term') AND status = 'active' "
    "AND processing_state = 'processed' AND source_state = 'active' AND superseded_by IS NULL "
    "AND EXISTS (SELECT 1 FROM cf_memory_projection_sources v WHERE v.uid = cf_memories.uid "
    "AND v.id = cf_memories.id AND v.operation = 'upsert')"
)


async def search_ids(env, uid, question, limit):
    vector = await asyncio.wait_for(embed_query(env, question), 30)
    summary, transcript = await asyncio.gather(
        query_vector_ids(env, 'CONVERSATION_VECTORS', uid, vector, top_k=limit),
        query_vector_ids(env, 'TRANSCRIPT_CHUNK_VECTORS', uid, vector, top_k=min(limit * 3, 100)),
        return_exceptions=True,
    )
    if isinstance(summary, BaseException):
        raise summary
    summary = await hydrate_candidate_ids(env, uid, 'conversation', summary)
    if isinstance(transcript, BaseException):
        record_fallback(
            component='other',
            from_mode='transcript_vectorize',
            to_mode='summary_vectorize',
            reason='dependency_unavailable',
            outcome='degraded',
        )
        transcript = []
    else:
        transcript = await hydrate_candidate_ids(env, uid, 'transcript_chunk', transcript)
    return list(dict.fromkeys(source_id for source_id, _ in transcript + summary))[:limit]


async def conversations(env, uid, ids):
    if not ids:
        return []
    result = (
        await env.APP_DB.prepare(
            f'SELECT {CONVERSATION_FIELDS} FROM cf_conversations WHERE uid = ? '
            f'AND id IN (SELECT value FROM json_each(?)) AND {CONVERSATION_VISIBLE}'
        )
        .bind(uid, json.dumps(ids))
        .all()
    )
    rows = {row['id']: row for row in result['results']}
    return [rows[identifier] for identifier in ids if identifier in rows]


async def facts(env, uid):
    result = (
        await env.APP_DB.prepare(
            f'SELECT {FACT_FIELDS} FROM cf_memories WHERE uid = ? AND {FACT_VISIBLE} '
            'ORDER BY updated_at DESC, id DESC LIMIT ?'
        )
        .bind(uid, PROMPT_MEMORY_LIMIT)
        .all()
    )
    return result['results']


def epoch(value):
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


def render_conversations(rows):
    values = []
    for row in rows:
        segments = json.loads(row['transcript_segments_json'])
        if not isinstance(segments, list) or any(
            not isinstance(segment, dict) or not isinstance(segment.get('text', ''), str) for segment in segments
        ):
            raise ValueError('invalid conversation transcript')
        values.append(
            SimpleNamespace(
                structured=SimpleNamespace(title=row['title'], overview=row['overview']),
                transcript_segments=[SimpleNamespace(text=segment.get('text', '')) for segment in segments],
                created_at=epoch(row['created_at']),
            )
        )
    return _ask_context_from_conversations(values)


def render_facts(rows, name):
    baseline, manual, generated = [], [], []
    for row in rows:
        memory = SimpleNamespace(content=row['content'], created_at=epoch(row['created_at']))
        (baseline if row['is_baseline'] else manual if row['manually_added'] else generated).append(memory)
    return _render_legacy_prompt_context(name, baseline, manual, generated)


async def still_current(env, uid, conversation_rows, fact_rows):
    current = await conversations(env, uid, [row['id'] for row in conversation_rows])
    if current != conversation_rows:
        return False
    if not fact_rows:
        return True
    result = (
        await env.APP_DB.prepare(
            f'SELECT {FACT_FIELDS} FROM cf_memories WHERE uid = ? AND {FACT_VISIBLE} '
            'AND id IN (SELECT value FROM json_each(?))'
        )
        .bind(uid, json.dumps([row['id'] for row in fact_rows]))
        .all()
    )
    return {row['id']: row for row in result['results']} == {row['id']: row for row in fact_rows}
