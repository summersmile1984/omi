"""Behavioral regression for the startup failures shipped in fork PR #7."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from fork import bootstrap, profile
from fork.patches.queue import _route_worker_auth
from fork.queue_config import QUEUES
from firestore_pg import migrations

ROOT = Path(__file__).resolve().parents[3]
SELF_HOST = {
    'name': 'self_hosted.local',
    'target': 'self_hosted',
    'stage': 'local',
    'identity_provider': 'better_auth',
    'capabilities': {'stt_providers': [], 'tts_provider': 'disabled', 'push_provider': 'disabled'},
    'data_plane': {
        'store': 'firestore_pg',
        'object_store': 'minio',
        'queue': 'redis',
        'cache': 'redis',
        'vector': 'qdrant',
    },
}


def child(code: str, **extra_env: str) -> subprocess.CompletedProcess:
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'backend'), **extra_env}
    return subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True, timeout=30)


@pytest.mark.parametrize('entry', ['import fork.main', 'from fork.worker import run; run(["--check"])'])
def test_unknown_profile_stops_real_entrypoint(entry):
    result = child(entry + '; print("WORKLOAD_RAN")', OMI_DEPLOYMENT_PROFILE='self_hosted.invalid')
    assert result.returncode != 0
    assert 'ProfileError' in result.stderr
    assert 'WORKLOAD_RAN' not in result.stdout


def test_self_host_admission_installs_firestore_facade_before_database_import():
    code = f'''from unittest import mock
from fork import bootstrap
from firestore_pg import migrations
with mock.patch.object(bootstrap.profile, 'current', return_value={SELF_HOST!r}), mock.patch.object(
    bootstrap, '_require_modules'
), mock.patch.object(migrations, 'check_schema'):
    bootstrap.bootstrap(bootstrap.Role.WORKER)
from database import _client
from firestore_pg.client import Client
assert _client.firestore.Client is Client
'''
    result = child(
        code,
        OMI_DEPLOYMENT_TARGET='self_hosted',
        OMI_ENV_STAGE='local',
        AUTH_PROVIDER='better_auth',
        QUEUE_BACKEND='redis',
        STORAGE_BACKEND='minio',
        FIRESTORE_PG_DSN='postgresql+psycopg://unused',
        REDIS_DB_HOST='localhost',
        REDIS_DB_PASSWORD='test-only',
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('role', ['API', 'WORKER', 'MEMORY_MAINTENANCE'])
def test_dependency_failure_stops_admission(role):
    code = f'''
from unittest import mock
from fork import bootstrap
with mock.patch.object(bootstrap.profile, 'current', return_value={SELF_HOST!r}), mock.patch.object(
    bootstrap.importlib, 'import_module', side_effect=ImportError('missing driver')
):
    bootstrap.bootstrap(bootstrap.Role.{role})
print('WORKLOAD_RAN')
'''
    result = child(
        code,
        OMI_DEPLOYMENT_TARGET='self_hosted',
        OMI_ENV_STAGE='local',
        AUTH_PROVIDER='better_auth',
        QUEUE_BACKEND='redis',
        STORAGE_BACKEND='minio',
        FIRESTORE_PG_DSN='postgresql+psycopg://unused',
        REDIS_DB_HOST='localhost',
        REDIS_DB_PASSWORD='test-only',
    )
    assert result.returncode != 0
    assert 'missing runtime dependency' in result.stderr
    assert 'WORKLOAD_RAN' not in result.stdout


def test_failed_patch_stops_before_upstream_app_is_imported():
    code = f'''
from unittest import mock
from fork import bootstrap
from fork.registry import Patch
broken = Patch('upstream-renamed', 'fork.profile', 'missing_upstream_symbol', lambda original: original,
               lambda row: True, 'PR #7 startup regression')
with mock.patch.object(bootstrap.profile, 'current', return_value={SELF_HOST!r}), mock.patch.object(
    bootstrap, 'collect', return_value=[broken]
):
    import fork.main
print('WORKLOAD_RAN')
'''
    result = child(
        code,
        OMI_DEPLOYMENT_TARGET='self_hosted',
        OMI_ENV_STAGE='local',
        AUTH_PROVIDER='better_auth',
        QUEUE_BACKEND='redis',
        STORAGE_BACKEND='minio',
        FIRESTORE_PG_DSN='postgresql+psycopg://unused',
        REDIS_DB_HOST='localhost',
        REDIS_DB_PASSWORD='test-only',
        ENCRYPTION_SECRET='x' * 32,
        AUTH_JWKS_URL='http://localhost/jwks',
        MINIO_ENDPOINT='http://localhost:9000',
        MINIO_PUBLIC_ENDPOINT='http://localhost:9000',
        MINIO_ACCESS_KEY='synthetic',
        MINIO_SECRET_KEY='synthetic',
    )
    assert result.returncode != 0
    assert 'PatchError' in result.stderr
    assert 'WORKLOAD_RAN' not in result.stdout


def test_worker_bootstrap_does_not_import_asgi_or_model_modules():
    with mock.patch.object(profile, 'current', return_value=SELF_HOST), mock.patch.object(
        bootstrap, '_require_modules'
    ), mock.patch.object(migrations, 'check_schema'), mock.patch.dict(
        os.environ,
        {
            'FIRESTORE_PG_DSN': 'postgresql+psycopg://unused',
            'REDIS_DB_HOST': 'localhost',
            'REDIS_DB_PASSWORD': 'test',
        },
        clear=True,
    ), mock.patch.object(
        bootstrap, 'collect', side_effect=AssertionError('API patch imported')
    ):
        bootstrap.bootstrap.cache_clear()
        try:
            admitted = bootstrap.bootstrap(bootstrap.Role.WORKER)
            assert admitted.patches == ()
        finally:
            bootstrap.bootstrap.cache_clear()


def test_memory_maintenance_bootstrap_uses_projection_patches_without_redis_or_asgi():
    from fork import capabilities
    from utils.memory import atom_keyword_index

    environment = {
        'FIRESTORE_PG_DSN': 'postgresql+psycopg://unused',
        'EMBEDDING_ENDPOINT': 'http://embedding:11434',
        'QDRANT_URL': 'http://qdrant:6333',
        'QDRANT_API_KEY': 'synthetic',
        'QDRANT_COLLECTION_PREFIX': 'synthetic',
        'TYPESENSE_HOST': 'typesense',
        'TYPESENSE_HOST_PORT': '8108',
        'TYPESENSE_API_KEY': 'synthetic',
        'MEMORY_TYPESENSE_COLLECTION': 'canonical_memory_atoms',
    }
    with mock.patch.object(profile, 'current', return_value=SELF_HOST), mock.patch.object(
        bootstrap, '_require_modules'
    ) as require_modules, mock.patch.object(migrations, 'check_schema'), mock.patch.object(
        capabilities, 'validate'
    ), mock.patch.object(
        bootstrap, 'collect_memory_projection', return_value=[]
    ) as collect_projection, mock.patch.object(
        bootstrap, 'collect', side_effect=AssertionError('API patches imported')
    ), mock.patch.object(
        atom_keyword_index, 'ensure_memories_collection'
    ) as ensure_collection, mock.patch.object(
        atom_keyword_index, 'ensure_ledger_keyword_schema'
    ) as ensure_ledger, mock.patch.dict(
        os.environ, environment, clear=True
    ):
        bootstrap.bootstrap.cache_clear()
        try:
            admitted = bootstrap.bootstrap(bootstrap.Role.MEMORY_MAINTENANCE)
        finally:
            bootstrap.bootstrap.cache_clear()

    assert admitted.role == bootstrap.Role.MEMORY_MAINTENANCE
    assert admitted.patches == ()
    collect_projection.assert_called_once_with()
    ensure_collection.assert_called_once_with()
    ensure_ledger.assert_called_once_with()
    imported = {name for call in require_modules.call_args_list for name in call.args[0]}
    assert 'redis' not in imported
    assert 'fastapi' not in imported


def test_migration_v8_admits_current_inventory_and_preserves_mapping():
    assert set(migrations.known_collections()) == migrations._declared_known_collections()
    assert migrations.LATEST_SCHEMA_VERSION == 8
    assert migrations.STATIC_HASHED_COLLECTION_IDS_V8 == {'frame_vision_receipts'}
    assert migrations.STATIC_HASHED_COLLECTION_IDS_V7 == {'feedback_events', 'feedback_reports'}
    assert migrations.STATIC_HASHED_COLLECTION_IDS_V6 == {
        'daily_memory_sweep_daily_summary_staged',
        'daily_memory_sweep_model_invocations',
        'daily_memory_sweep_onboarding_sources',
        'daily_memory_sweep_onboarding_staged',
        'daily_memory_sweep_receipts',
        'daily_memory_sweep_sources',
        'jit_proactivity_candidate_turns',
        'jit_proactivity_daily_budgets',
        'jit_proactivity_events',
        'jit_trigger_feedback',
        'memory_deletion_receipts',
        'memory_ledger_reopens',
    }
    assert migrations.STATIC_HASHED_COLLECTION_IDS_V5 == {'onboarding_admission'}
    assert migrations.STATIC_HASHED_COLLECTION_IDS_V4 == {'legal_holds', 'legal_hold_deletion_gates'}
    assert migrations.STATIC_HASHED_COLLECTION_IDS_V3 == {
        'chat_first_dead_letters',
        'conversation_keyframe_jobs',
        'frame_requests',
    }
    assert migrations.collection_table_name('users') == 'users'
    assert migrations.collection_table_name('frame_requests').startswith('f_')


def test_new_collection_without_schema_version_is_rejected():
    with mock.patch.object(
        migrations,
        '_declared_known_collections',
        return_value={
            *migrations.known_collections(),
            'unversioned_future_workflow',
        },
    ):
        with pytest.raises(migrations.SchemaNotCurrent, match='new statically-known collections'):
            migrations.known_collections()


def test_new_typed_memory_collection_without_schema_version_is_rejected():
    original_paths = migrations.MemoryCollections.all_collection_paths

    def with_future_path(collections):
        return [*original_paths(collections), f'{collections.user_root}/future_memory_owner']

    with mock.patch.object(migrations.MemoryCollections, 'all_collection_paths', with_future_path):
        with pytest.raises(migrations.SchemaNotCurrent, match='future_memory_owner'):
            migrations.known_collections()


@pytest.mark.parametrize('queue', QUEUES, ids=lambda q: q.name)
def test_queue_secret_is_scoped_to_the_handler(queue):
    secrets = {q.secret_env: q.name.ljust(40, 'x') for q in QUEUES}
    verify = _route_worker_auth(None)

    def request(path, token):
        return Request(
            {
                'type': 'http',
                'path': path,
                'headers': [(b'x-omi-queue-secret', token.encode())],
                'scheme': 'http',
                'server': ('backend', 8080),
                'query_string': b'',
            }
        )

    with mock.patch.dict(os.environ, secrets, clear=True):
        assert verify(request(queue.path, secrets[queue.secret_env])) == 0
        other = next(q for q in QUEUES if q != queue)
        with pytest.raises(HTTPException) as error:
            verify(request(queue.path, secrets[other.secret_env]))
        assert error.value.status_code == 403
        with pytest.raises(HTTPException):
            verify(request('/unowned/internal/path', secrets[queue.secret_env]))


def test_migration_cli_never_defaults_to_an_unconfigured_database():
    result = child('from fork.migrate import main; main(["migrate"])', FIRESTORE_PG_DSN='')
    assert result.returncode == 2
    assert 'FIRESTORE_PG_DSN is required' in result.stderr


@pytest.mark.parametrize('selected', ['self_hosted.production', 'self_hosted.beta', 'self_hosted.local'])
def test_canonical_profile_retains_existing_egress_denial(selected):
    from fork.egress_policy import EgressPolicyUnavailable, assert_http_endpoint_allowed

    with mock.patch.dict(os.environ, {'OMI_DEPLOYMENT_PROFILE': selected}, clear=True):
        with pytest.raises(EgressPolicyUnavailable, match='official_endpoint_forbidden'):
            assert_http_endpoint_allowed('https://api.openai.com/v1/models')
        assert assert_http_endpoint_allowed('http://backend:8080/health') == 'backend'


def test_supervisor_fails_container_when_a_consumer_exits():
    from fork import worker
    from utils import cloud_tasks_redis

    consumers = [mock.Mock() for _ in QUEUES]
    for consumer in consumers:
        consumer.poll.return_value = None
    consumers[1].poll.return_value = 0
    with mock.patch.object(worker, 'bootstrap'), mock.patch.object(type(QUEUES[0]), 'validate'), mock.patch.object(
        cloud_tasks_redis, '_r'
    ), mock.patch.object(worker.subprocess, 'Popen', side_effect=consumers):
        assert worker.run([]) == 1
    consumers[0].terminate.assert_called_once()
    consumers[2].terminate.assert_called_once()
    consumers[3].terminate.assert_called_once()
    for consumer in consumers:
        consumer.wait.assert_called_once_with(timeout=5)


def test_queue_configuration_rejects_wrong_destination_and_short_secret():
    queue = QUEUES[0]
    with mock.patch.dict(
        os.environ, {queue.handler_env: 'http://backend:8080' + queue.path, queue.secret_env: 'x' * 32}, clear=True
    ):
        queue.validate()
        os.environ[queue.secret_env] = 'short'
        with pytest.raises(profile.ProfileError, match='requires at least 32'):
            queue.validate()
        os.environ[queue.secret_env] = 'x' * 32
        os.environ[queue.handler_env] = 'http://backend:invalid' + queue.path
        with pytest.raises(profile.ProfileError, match='valid HTTP'):
            queue.validate()


@pytest.mark.parametrize('queue', QUEUES, ids=lambda q: q.name)
def test_worker_dispatches_with_the_selected_route_and_secret(queue):
    from fork import worker
    from utils import cloud_tasks_redis

    redis = mock.Mock()
    redis.blpop.side_effect = [
        ('test', json.dumps({'task_id': 'synthetic', 'payload': {'job_id': 'synthetic'}})),
        KeyboardInterrupt,
    ]
    env = {q.handler_env: 'http://backend:8080' + q.path for q in QUEUES}
    env.update({q.secret_env: q.name.ljust(40, 'x') for q in QUEUES})
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(worker, 'bootstrap'), mock.patch.object(
        cloud_tasks_redis, '_r', return_value=redis
    ), mock.patch.object(cloud_tasks_redis.httpx, 'post', return_value=mock.Mock(status_code=200)) as post:
        with pytest.raises(KeyboardInterrupt):
            worker.run(['--queue', queue.name])
        post.assert_called_once_with(
            env[queue.handler_env],
            json={'job_id': 'synthetic'},
            headers={'X-Omi-Queue-Secret': env[queue.secret_env], 'X-Omi-Queue-Retry-Count': '0'},
            timeout=30.0,
        )


def test_legacy_unconfigured_api_keeps_upstream_admission():
    with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
        bootstrap, 'collect', side_effect=AssertionError('legacy cloud profile was patched')
    ):
        profile.reset()
        bootstrap.bootstrap.cache_clear()
        try:
            admission = bootstrap.bootstrap(bootstrap.Role.API)
            assert admission.target == 'omi_cloud'
            assert admission.patches == ()
        finally:
            profile.reset()
            bootstrap.bootstrap.cache_clear()
