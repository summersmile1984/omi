"""Developer ask: scoped key admission, canonical retrieval and the original RAG prompt."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from developer_ask_contract import DeveloperAskRequest, DeveloperAskResponse, DeveloperAskSource, _ASK_NO_CONTEXT
from developer_ask_prompt import render_prompt
import developer_ask_context as context
from developer_routes import _authenticate
from fallback import record_fallback
from mcp_routes import _contact_profile
from synthesis_routes import _rpc_mapping

router = APIRouter()
DEFAULT_ASK_MODEL = '@cf/meta/llama-3.3-70b-instruct-fp8-fast'


async def principal(request: Request):
    owner, denial = await _authenticate(request, 'conversations:read')
    if denial is not None:
        # Preserve the shared Developer authentication wire shape.
        import json

        body = json.loads(denial.body)
        raise HTTPException(denial.status_code, body.get('detail', body.get('error')))
    return owner


async def current_principal(request, owner):
    current, denial = await _authenticate(request, 'conversations:read')
    return denial is None and current == owner


def unavailable(reason='dependency_unavailable'):
    record_fallback(
        component='llm',
        from_mode='workers_ai',
        to_mode='none',
        reason=reason,
        outcome='exhausted',
    )
    return HTTPException(503, 'Answer temporarily unavailable')


async def record_usage(env, uid, model, result):
    usage = _rpc_mapping(result.get('usage')) or {}
    counts = [usage.get(key) for key in ('prompt_tokens', 'completion_tokens')]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 9_007_199_254_740_991
        for value in counts
    ):
        raise ValueError('invalid answer usage')
    await env.APP_DB.prepare(
        'INSERT INTO cf_llm_usage_daily (uid, usage_day, usage_kind, feature, model, account, '
        'input_tokens, output_tokens, total_tokens, call_count, updated_at) '
        "SELECT ?, ?, 'feature', 'chat', ?, 'cloudflare', ?, ?, ?, 1, ? "
        'WHERE NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) '
        'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?) '
        'ON CONFLICT(uid, usage_day, usage_kind, feature, model, account) DO UPDATE SET '
        'input_tokens = input_tokens + excluded.input_tokens, output_tokens = output_tokens + excluded.output_tokens, '
        'total_tokens = total_tokens + excluded.total_tokens, call_count = call_count + 1, updated_at = excluded.updated_at'
    ).bind(
        uid, datetime.now(timezone.utc).date().isoformat(), model, *counts, sum(counts), int(time.time()), uid, uid
    ).run()


@router.post('/v1/dev/user/ask', response_model=DeveloperAskResponse, operation_id='ask')
async def ask(request: Request, body: DeveloperAskRequest, response: Response, owner=Depends(principal)):
    response.headers['cache-control'] = 'no-store'
    question = body.question.strip()
    if not question:
        raise HTTPException(400, 'question must not be empty')
    env, uid = request.scope['env'], owner.uid
    try:
        ids = await asyncio.wait_for(context.search_ids(env, uid, question, body.limit), 45)
        rows = await context.conversations(env, uid, ids)
    except Exception as error:
        raise HTTPException(503, 'Search temporarily unavailable') from error
    if not rows:
        return DeveloperAskResponse(answer=_ASK_NO_CONTEXT, sources=[])
    try:
        fact_rows = await context.facts(env, uid)
        profile = await asyncio.wait_for(_contact_profile(request, uid), 15)
        prompt = render_prompt(
            uid,
            question,
            context.render_conversations(rows),
            context.render_facts(fact_rows, profile.get('name')),
            body.timezone,
        )
        # Snapshot verification occurs after all awaited enrichment, immediately
        # before provider disclosure, and again before returning generated text.
        if not await current_principal(request, owner) or not await context.still_current(env, uid, rows, fact_rows):
            raise HTTPException(409, 'Answer sources or permissions changed. Try again.')
        model = getattr(env, 'WORKERS_AI_DEVELOPER_ASK_MODEL', DEFAULT_ASK_MODEL)
        result = _rpc_mapping(
            await asyncio.wait_for(
                env.AI.run(
                    model,
                    {'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 2048, 'temperature': 0},
                ),
                90,
            )
        )
        if not isinstance(result, dict):
            raise ValueError('invalid answer response')
        await record_usage(env, uid, model, result)
        answer = result.get('response')
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('empty answer response')
        references = re.findall(r'\[(\d+)\]', answer)
        if not references or any(len(ref) > 2 or not 1 <= int(ref) <= len(rows) for ref in references):
            raise ValueError('answer references do not resolve to its sources')
        if not await current_principal(request, owner) or not await context.still_current(env, uid, rows, fact_rows):
            raise HTTPException(409, 'Answer sources or permissions changed. Try again.')
        return DeveloperAskResponse(
            answer=answer,
            sources=[
                DeveloperAskSource(
                    id=row['id'],
                    title=(row['title'] or 'Untitled').strip(),
                    created_at=context.epoch(row['created_at']),
                )
                for row in rows
            ],
        )
    except HTTPException:
        raise
    except Exception as error:
        raise unavailable('malformed_doc' if isinstance(error, ValueError) else 'dependency_unavailable') from error
