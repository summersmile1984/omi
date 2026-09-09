"""Compile one image's public LLM profile into the serving process environment."""

import argparse
import json
from pathlib import Path
import shlex

from .model_contract import validate_llm


def process_environment(contract):
    return {
        'AGENT_STREAM_FIRST_EVENT_TIMEOUT_SECONDS': str(contract.request_timeout_seconds),
        'AGENT_STREAM_MAX_DURATION_SECONDS': str(2 * contract.request_timeout_seconds),
        'QUEUE_REDIS_FINALIZATION_REQUEST_TIMEOUT_SECONDS': str(4 * contract.request_timeout_seconds),
    }


def environment(table):
    rows = table.get('profiles', {})
    if table.get('target') != 'self_hosted' or len(rows) != 1:
        raise ValueError('LLM image requires exactly one generated self-host stage')
    row = next(iter(rows.values()))
    contract = validate_llm(row.get('llm'))
    if contract is None or row.get('capabilities', {}).get('llm_provider') != contract.provider:
        raise ValueError('LLM image requires an enabled, consistent model contract')
    return {
        'OLLAMA_HOST': '0.0.0.0:11434',
        'OLLAMA_MODELS': '/models',
        'OLLAMA_NO_CLOUD': '1',
        'OLLAMA_KEEP_ALIVE': '0',
        'OLLAMA_NUM_PARALLEL': str(contract.parallel_requests),
        'OLLAMA_MAX_LOADED_MODELS': '1',
        'OLLAMA_CONTEXT_LENGTH': str(contract.context_window),
        'OLLAMA_FLASH_ATTENTION': '1',
        'OLLAMA_KV_CACHE_TYPE': contract.kv_cache_type,
    }


def compile_environment(table):
    return '\n'.join('export ' + name + '=' + shlex.quote(value) for name, value in environment(table).items()) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', type=Path)
    args = parser.parse_args()
    print(compile_environment(json.loads(args.profile.read_text())), end='')


if __name__ == '__main__':
    main()
