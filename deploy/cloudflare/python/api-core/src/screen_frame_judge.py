"""Original screenshot privacy prompt and schema over Cloudflare's Qwen vision binding."""

import asyncio
import base64
from datetime import datetime, timezone
import time

from fallback import record_fallback
from screen_frames_contract import ScreenFrameJudgement
from screen_frames_prompt import _PRIVACY_PROMPT
from synthesis_routes import _rpc_mapping

SCREEN_FRAME_MODEL = '@cf/qwen/qwen3.8-27b'


class JudgeFailure(Exception):
    pass


def model_input(jpeg):
    return {
        'messages': [
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': _PRIVACY_PROMPT},
                    {
                        'type': 'image_url',
                        'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode('ascii')},
                    },
                ],
            }
        ],
        'response_format': {
            'type': 'json_schema',
            'json_schema': {'name': 'ScreenFrameJudgement', 'schema': ScreenFrameJudgement.model_json_schema()},
        },
        'max_completion_tokens': 8192,
        'n': 1,
        'temperature': 0,
    }


async def _usage(env, attempt, model, result):
    usage = _rpc_mapping(result.get('usage')) or {}

    def count(key):
        value = usage.get(key)
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9_007_199_254_740_991
            else 0
        )

    now = int(time.time())
    incoming, outgoing = count('prompt_tokens'), count('completion_tokens')
    await env.APP_DB.prepare(
        'INSERT INTO cf_llm_usage_daily (uid, usage_day, usage_kind, feature, model, account, '
        'input_tokens, output_tokens, total_tokens, call_count, updated_at) '
        "SELECT ?, ?, 'feature', 'screen_frame_judge', ?, 'cloudflare', ?, ?, ?, 1, ? "
        'WHERE EXISTS (SELECT 1 FROM cf_screen_frame_attempts WHERE uid = ? AND attempt_id = ? '
        'AND response_json IS NULL AND expires_at > unixepoch()) '
        'ON CONFLICT(uid, usage_day, usage_kind, feature, model, account) DO UPDATE SET '
        'input_tokens = input_tokens + excluded.input_tokens, output_tokens = output_tokens + excluded.output_tokens, '
        'total_tokens = total_tokens + excluded.total_tokens, call_count = call_count + 1, updated_at = excluded.updated_at'
    ).bind(
        attempt['uid'],
        datetime.fromtimestamp(now, timezone.utc).date().isoformat(),
        model,
        incoming,
        outgoing,
        incoming + outgoing,
        now,
        attempt['uid'],
        attempt['attempt_id'],
    ).run()


async def judge(env, attempt, jpeg):
    try:
        result = _rpc_mapping(await asyncio.wait_for(env.AI.run(SCREEN_FRAME_MODEL, model_input(jpeg)), timeout=90))
        if not result:
            raise JudgeFailure('malformed_output')
        await _usage(env, attempt, SCREEN_FRAME_MODEL, result)
        choices = result.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise JudgeFailure('malformed_output')
        message = choices[0].get('message')
        if (
            choices[0].get('finish_reason') != 'stop'
            or not isinstance(message, dict)
            or message.get('role') != 'assistant'
            or message.get('refusal')
            or message.get('tool_calls')
        ):
            raise JudgeFailure('malformed_output')
        text = message.get('content')
        if not isinstance(text, str):
            raise JudgeFailure('malformed_output')
        verdict = ScreenFrameJudgement.model_validate_json(text)
        if (verdict.outcome == 'approved_clean') != (verdict.reject_reason is None):
            raise JudgeFailure('contradictory_output')
        return verdict
    except Exception as error:
        record_fallback(
            component='llm',
            from_mode='workers_ai',
            to_mode='none',
            reason='dependency_unavailable',
            outcome='exhausted',
        )
        raise JudgeFailure('judge_call_failed') from error
