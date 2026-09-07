"""Original consolidation messages over Workers AI, followed by canonical apply.

The caller owns candidate discovery, retry leases and recurrence handoff. This
module never supplies decisions when inference, parsing or source validation
fails, and does not register an unleased public/background trigger.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
from types import FunctionType

from fallback import record_fallback
from memory_apply_intake import load_memory_control
from memory_consolidation_apply import MAX_CONSOLIDATION_BATCH_ITEMS, _hydrate_context, apply_consolidation_batch
from memory_kernel_consolidation import ConsolidationAgentBatch, build_consolidation_llm_messages
from memory_consolidation_context import gather_consolidation_context
from memory_consolidation_leases import verify_consolidation_leases
from memory_kernel_consolidation_schema import CONSOLIDATION_OUTPUT_SCHEMA
from synthesis_routes import _rpc_mapping, _structured_json

DEFAULT_CONSOLIDATION_MODEL = '@cf/qwen/qwen3.8-27b'
MAX_INPUT_BYTES = 110_000
MAX_OUTPUT_BYTES = 256_000
MODEL_TIMEOUT_SECONDS = 180

# Verbatim format-instructions text from langchain-core==1.3.3 (MIT), matching
# backend/requirements.txt. The original model prompt is staged from its owner.
_FORMAT_INSTRUCTIONS = 'The output should be formatted as a JSON instance that conforms to the JSON schema below.\n\nAs an example, for the schema {{"properties": {{"foo": {{"title": "Foo", "description": "a list of strings", "type": "array", "items": {{"type": "string"}}}}}}, "required": ["foo"]}}\nthe object {{"foo": ["bar", "baz"]}} is a well-formatted instance of the schema. The object {{"properties": {{"foo": ["bar", "baz"]}}}} is not well-formatted.\n\nHere is the output schema:\n```\n{schema}\n```'


@dataclass(frozen=True)
class _MessageContent:
    content: object


class _SchemaInstructions:
    def __init__(self, *, pydantic_object):
        if pydantic_object is not ConsolidationAgentBatch:
            raise ValueError('unexpected consolidation schema')

    def get_format_instructions(self):
        schema = deepcopy(CONSOLIDATION_OUTPUT_SCHEMA)
        schema.pop('title', None)
        schema.pop('type', None)
        return _FORMAT_INSTRUCTIONS.format(schema=json.dumps(schema, ensure_ascii=False))


def model_messages(context):
    # Supply only the provider-neutral containers/schema formatter expected by
    # the unchanged message constructor. Invocation-local globals prevent one
    # concurrent request from replacing another request's context or adapter.
    render = FunctionType(
        build_consolidation_llm_messages.__code__,
        {
            **build_consolidation_llm_messages.__globals__,
            'PydanticOutputParser': _SchemaInstructions,
            'SystemMessage': _MessageContent,
            'HumanMessage': _MessageContent,
        },
    )
    system, user = render(context)
    (block,) = system.content
    messages = [
        {'role': 'system', 'content': block['text']},
        {'role': 'user', 'content': user.content},
    ]
    # Workers AI accepts text messages. Its cache is provider-owned; the GPT
    # breakpoint metadata is not sent as a nonportable request parameter.
    if sum(len(message['content'].encode('utf-8')) for message in messages) > MAX_INPUT_BYTES:
        raise ConsolidationInferenceError('input_too_large')
    return messages


class ConsolidationInferenceError(Exception):
    """Non-content failure suitable for the maintenance retry owner."""


async def _record_usage(env, uid, model, result):
    usage = _rpc_mapping(result.get('usage')) or {}
    counts = [usage.get(key) for key in ('prompt_tokens', 'completion_tokens')]
    if any(type(value) is not int or not 0 <= value <= 9_007_199_254_740_991 for value in counts):
        raise ConsolidationInferenceError('invalid_usage')
    now = int(time.time())
    await env.APP_DB.prepare(
        'INSERT INTO cf_llm_usage_daily (uid, usage_day, usage_kind, feature, model, account, '
        'input_tokens, output_tokens, total_tokens, call_count, updated_at) '
        "SELECT ?, ?, 'feature', 'memory_consolidation', ?, 'cloudflare', ?, ?, ?, 1, ? "
        'WHERE NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) '
        'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?) '
        'ON CONFLICT(uid, usage_day, usage_kind, feature, model, account) DO UPDATE SET '
        'input_tokens = input_tokens + excluded.input_tokens, output_tokens = output_tokens + excluded.output_tokens, '
        'total_tokens = total_tokens + excluded.total_tokens, call_count = call_count + 1, updated_at = excluded.updated_at'
    ).bind(
        uid, datetime.fromtimestamp(now, timezone.utc).date().isoformat(), model, *counts, sum(counts), now, uid, uid
    ).run()


async def _infer(env, context):
    messages = model_messages(context)
    model = getattr(env, 'WORKERS_AI_MEMORY_CONSOLIDATION_MODEL', DEFAULT_CONSOLIDATION_MODEL)
    # Match upstream invoke(messages) followed by Pydantic validation. Adding
    # provider JSON-mode constraints changed the observed Qwen completion.
    try:
        response = await asyncio.wait_for(
            env.AI.run(
                model,
                {
                    'messages': messages,
                    'max_completion_tokens': 8192,
                    'n': 1,
                    'reasoning_effort': 'medium',
                    'temperature': 0,
                },
            ),
            timeout=MODEL_TIMEOUT_SECONDS,
        )
    except Exception as error:
        record_fallback(
            component='llm',
            from_mode='workers_ai',
            to_mode='none',
            reason='dependency_unavailable',
            outcome='exhausted',
        )
        raise ConsolidationInferenceError('invoke_failed') from error
    try:
        result = _rpc_mapping(response)
        if result is None:
            raise ConsolidationInferenceError('invalid_response')
        await _record_usage(env, context.uid, model, result)
        choices = result.get('choices')
        if not isinstance(choices, list) or len(choices) != 1:
            raise ConsolidationInferenceError('invalid_choices')
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get('finish_reason') != 'stop':
            raise ConsolidationInferenceError('incomplete_completion')
        message = choice.get('message')
        if not isinstance(message, dict) or message.get('role') != 'assistant' or message.get('refusal'):
            raise ConsolidationInferenceError('invalid_completion')
        raw = message.get('content')
        if len(json.dumps(raw, ensure_ascii=False).encode('utf-8')) > MAX_OUTPUT_BYTES:
            raise ConsolidationInferenceError('output_too_large')
        parsed = _structured_json(raw) if isinstance(raw, str) else None
        if not isinstance(parsed, dict):
            raise ConsolidationInferenceError('invalid_json')
        return ConsolidationAgentBatch.model_validate(parsed)
    except Exception as error:
        record_fallback(
            component='llm', from_mode='workers_ai', to_mode='none', reason='malformed_doc', outcome='exhausted'
        )
        raise ConsolidationInferenceError('output_failed') from error


async def consolidate_pending_with_llm(env, uid, memory_ids, *, run_id, leases=()):
    """Caller-owned leased work: retrieve authoritative candidates before inference."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError('invalid consolidation run')
    context = await gather_consolidation_context(env, uid, memory_ids)
    return await consolidate_with_llm(env, context, run_id=run_id, leases=leases)


async def consolidate_with_llm(env, context, *, run_id, leases=()):
    if not run_id.strip() or not 1 <= len(context.pending_items) <= MAX_CONSOLIDATION_BATCH_ITEMS:
        raise ValueError('invalid consolidation run')
    if len({item.memory_id for item in context.pending_items}) != len(context.pending_items):
        raise ValueError('duplicate consolidation source')
    # Validate owner/source snapshots before provider disclosure. Apply reloads
    # them after inference and rejects a concurrent correction or deletion.
    _, control = await load_memory_control(env, context.uid)
    _, _, current = await _hydrate_context(env, context, control.account_generation)
    _, before_disclosure = await load_memory_control(env, context.uid)
    if before_disclosure != control:
        raise ConsolidationInferenceError('authority_changed')
    await verify_consolidation_leases(env, leases, current.pending_items, control)
    batch = await _infer(env, current)
    return await apply_consolidation_batch(
        env, current, batch, run_id=run_id, now=datetime.now(timezone.utc), leases=leases
    )


# Format-instructions text and schema reduction are derived from langchain-core
# 1.3.3. MIT License, Copyright (c) LangChain, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
