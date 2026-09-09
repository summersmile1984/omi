"""Explicit process admission shared by the API and queue worker.

Importing this module has no side effects. Entrypoints call bootstrap before
importing their workload; an exception therefore terminates the real process.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

from . import profile
from .patches import collect, collect_memory_projection
from .registry import build_registry


class Role(str, Enum):
    API = 'api'
    WORKER = 'worker'
    MEMORY_MAINTENANCE = 'memory_maintenance'


@dataclass(frozen=True)
class Admission:
    name: str
    target: str
    role: Role
    patches: tuple[str, ...]


def _require(name: str, minimum: int = 1) -> str:
    value = os.environ.get(name, '').strip()
    if len(value) < minimum:
        raise profile.ProfileError(f'{name} is required (minimum {minimum} characters)')
    return value


def _bind(name: str, value: str) -> None:
    existing = os.environ.get(name, '').strip()
    if existing and existing != value:
        raise profile.ProfileError(f'{name} conflicts with the selected deployment profile')
    os.environ[name] = value


def _require_modules(names: tuple[str, ...]) -> None:
    for name in names:
        try:
            importlib.import_module(name)
        except ImportError as error:
            raise profile.ProfileError(f'missing runtime dependency: {name}') from error


@lru_cache(maxsize=None)
def bootstrap(role: Role = Role.API) -> Admission:
    row = profile.current()
    if row['target'] == 'omi_cloud':
        if role != Role.API:
            raise profile.ProfileError('fork background workers require a self_hosted profile')
        return Admission(row['name'], row['target'], role, ())
    if row['target'] != 'self_hosted':
        raise profile.ProfileError('this Python runtime supports self_hosted; cloudflare runs Workers')

    expected = {
        'store': 'firestore_pg',
        'object_store': 'minio',
        'queue': 'redis',
        'cache': 'redis',
        'vector': 'qdrant',
    }
    for name, value in expected.items():
        if row.get('data_plane', {}).get(name) != value:
            raise profile.ProfileError(f'self_hosted data_plane.{name} must be {value}')
    _bind('OMI_DEPLOYMENT_TARGET', 'self_hosted')
    stages = {'production': 'prod', 'beta': 'dev', 'local': 'local'}
    if row.get('stage') not in stages:
        raise profile.ProfileError('self_hosted requires a production, beta, or local stage')
    _bind('OMI_ENV_STAGE', stages[row['stage']])
    _bind('AUTH_PROVIDER', row['identity_provider'])
    _bind('STORAGE_BACKEND', 'minio')
    _bind('QUEUE_BACKEND', 'redis')
    _require('FIRESTORE_PG_DSN')
    _require_modules(('sqlalchemy', 'psycopg', 'httpx'))
    # Must precede any upstream database import: the facade replaces the SDK
    # module aliases that captured business modules resolve through.
    from firestore_pg.compat import install as install_firestore_facade

    install_firestore_facade()

    if role in (Role.API, Role.WORKER):
        _require('REDIS_DB_HOST')
        _require('REDIS_DB_PASSWORD')
        _require_modules(('redis',))

    from .operator_ai import select as select_operator_ai

    operator_ai = select_operator_ai(row)
    if row.get('llm') or operator_ai:
        from .model_contract import validate_llm
        from .llm_runtime import process_environment

        llm = operator_ai or validate_llm(row['llm'])
        # Cloud-provider 25/60-second defaults are not CPU-model budgets. Bind
        # before imports so captured chat owners share the selected deadline.
        for name, value in process_environment(llm).items():
            _bind(name, value)

    if role == Role.API:
        from .capabilities import validate as validate_capabilities

        validate_capabilities(row)
        for name, value in {
            'SPEAKER_EMBEDDING_PROVIDER': 'disabled',
            'TTS_PROVIDER': 'mimo' if operator_ai else 'kokoro' if row.get('speech') else 'disabled',
            'PUSH_PROVIDER': 'disabled',
            'OMI_LLM_GATEWAY_FEATURE_MODE': 'off',
            'OMI_LLM_CHAT_AGENT_ROUTE': 'direct',
            'OMI_LLM_GATEWAY_DEV_SHADOW_ALL_ENABLED': '0',
            'OMI_LLM_GATEWAY_CONVERSATION_STRUCTURE_SHADOW_ENABLED': '0',
            'OMI_LLM_GATEWAY_CONVERSATION_ACTION_ITEMS_SHADOW_ENABLED': '0',
            'STT_SERVICE_MODELS': 'mimo' if operator_ai else 'sensevoice' if row.get('speech') else 'disabled',
            'STT_PRERECORDED_MODEL': 'mimo' if operator_ai else 'sensevoice' if row.get('speech') else 'disabled',
            'SENSEVOICE_SPEAKER_MODE': 'single_speaker',
        }.items():
            _bind(name, value)
        if row.get('speech'):
            from pathlib import Path

            store = _require('SPEECH_MODEL_STORE')
            if not Path(store).is_absolute():
                raise profile.ProfileError('SPEECH_MODEL_STORE must be absolute')
            for name, value in {
                'SENSEVOICE_MODEL_DIR': str(Path(store) / row['speech']['stt_model']),
                'SENSEVOICE_NUM_THREADS': '2',
                'SENSEVOICE_USE_ITN': '1',
                'SENSEVOICE_STREAM_WINDOW_SECONDS': '5.0',
                'SENSEVOICE_STREAM_POLL_SECONDS': '0.1',
                'HOSTED_VAD_API_URL': '',
            }.items():
                _bind(name, value)
        if row.get('llm'):
            from .local_llm import contract_for_profile

            contract_for_profile()
            _require('LLM_ENDPOINT')
        if operator_ai:
            from .operator_ai import credentials

            credentials()
        _require('ENCRYPTION_SECRET', 32)
        _require('AUTH_JWKS_URL')
        _bind('VECTOR_STORE_PROVIDER', 'qdrant')
        if os.environ.get('PINECONE_API_KEY') or os.environ.get('PINECONE_INDEX_NAME'):
            raise profile.ProfileError('Pinecone configuration conflicts with the self-host Qdrant authority')
        _require_modules(('jwt', 'boto3'))
        from .storage_minio import Config as ObjectConfig

        ObjectConfig.from_env()
        registry = build_registry(collect()).apply(row)
        applied = tuple(registry.applied)
        if row.get('llm') or operator_ai:
            from .local_llm import check as check_llm

            check_llm()
        if row.get('speech'):
            from .speech import check as check_speech

            check_speech()
        from .queue_config import QUEUES

        for queue in QUEUES:
            queue.validate()
    elif role == Role.WORKER:
        # The consumer uses Redis directly, not the upstream producer/storage
        # factories. Do not import ASGI routers and model providers in a worker.
        applied = ()
    else:
        from .capabilities import validate as validate_capabilities

        validate_capabilities(row)
        _bind('VECTOR_STORE_PROVIDER', 'qdrant')
        _bind('MEMORY_KEYWORD_INDEX_PROVIDER', 'typesense')
        if os.environ.get('PINECONE_API_KEY') or os.environ.get('PINECONE_INDEX_NAME'):
            raise profile.ProfileError('Pinecone configuration conflicts with the self-host Qdrant authority')
        for name in ('EMBEDDING_ENDPOINT', 'QDRANT_URL', 'QDRANT_API_KEY', 'QDRANT_COLLECTION_PREFIX'):
            _require(name)
        for name in ('TYPESENSE_HOST', 'TYPESENSE_HOST_PORT', 'TYPESENSE_API_KEY', 'MEMORY_TYPESENSE_COLLECTION'):
            _require(name)
        _require_modules(('typesense',))
        registry = build_registry(collect_memory_projection()).apply(row)
        applied = tuple(registry.applied)
        from utils.memory.atom_keyword_index import ensure_ledger_keyword_schema, ensure_memories_collection

        ensure_memories_collection()
        ensure_ledger_keyword_schema()

    from firestore_pg.migrations import check_schema

    check_schema()
    result = Admission(row['name'], row['target'], role, applied)
    logging.getLogger(__name__).info('fork admitted %s role=%s patches=%s', result.name, role.value, applied)
    return result
