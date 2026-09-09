"""Unchanged upstream attention messages and output model over Workers AI."""

import asyncio
import hashlib
import json

from fallback import record_fallback
from recommendation_kernel import JudgmentOutput, judgment_messages
from synthesis_routes import _rpc_mapping, _structured_json

DEFAULT_MODEL = '@cf/qwen/qwen3.8-27b'


class RecommendationInferenceError(RuntimeError):
    pass


class LiveJudgment:
    def __init__(self, env):
        self.env = env
        self.model = getattr(env, 'WORKERS_AI_TASK_INTELLIGENCE_MODEL', DEFAULT_MODEL)
        # StableId forbids @ and /; retain the exact provider model in usage receipts.
        self.model_version = 'workers-ai:' + hashlib.sha256(self.model.encode()).hexdigest()
        self.receipt = None

    async def judge(self, subjects):
        if not subjects:
            return []
        messages = judgment_messages(subjects)
        request = {
            'messages': messages,
            'response_format': {
                'type': 'json_schema',
                'json_schema': {'name': 'JudgmentOutput', 'schema': JudgmentOutput.model_json_schema()},
            },
            'max_completion_tokens': 8192,
            'n': 1,
            'temperature': 0,
        }
        if len(json.dumps(request, ensure_ascii=False).encode()) > 110000:
            raise RecommendationInferenceError('input_too_large')
        try:
            raw = await asyncio.wait_for(self.env.AI.run(self.model, request), timeout=180)
        except Exception as error:
            record_fallback(
                component='llm',
                from_mode='workers_ai',
                to_mode='none',
                reason='dependency_unavailable',
                outcome='exhausted',
            )
            raise RecommendationInferenceError('provider_unavailable') from error
        try:
            result = _rpc_mapping(raw)
            choices = result.get('choices') if result else None
            if not isinstance(choices, list) or len(choices) != 1 or choices[0].get('finish_reason') != 'stop':
                raise ValueError('incomplete response')
            message = choices[0].get('message')
            if not isinstance(message, dict) or message.get('role') != 'assistant' or message.get('refusal'):
                raise ValueError('invalid message')
            content = message.get('content')
            if not isinstance(content, str) or len(content.encode()) > 256000:
                raise ValueError('invalid content')
            output = JudgmentOutput.model_validate(_structured_json(content))
            usage = _rpc_mapping(result.get('usage')) or {}
            counts = [usage.get(key) for key in ('prompt_tokens', 'completion_tokens')]
            if any(type(value) is not int or not 0 <= value <= 9_007_199_254_740_991 for value in counts):
                raise ValueError('invalid usage')
            self.receipt = {
                'model': self.model,
                'input_tokens': counts[0],
                'output_tokens': counts[1],
                'request_hash': hashlib.sha256(
                    json.dumps(request, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
                'response_hash': hashlib.sha256(content.encode()).hexdigest(),
            }
            return output.selections
        except Exception as error:
            record_fallback(
                component='llm', from_mode='workers_ai', to_mode='none', reason='malformed_doc', outcome='exhausted'
            )
            raise RecommendationInferenceError('invalid_output') from error
