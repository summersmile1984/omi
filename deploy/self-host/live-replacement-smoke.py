#!/usr/bin/env python3
"""Exercise the live self-host identity/data/queue/object/vector closure.

Run this inside the production backend image so it uses the exact configured
PostgreSQL, Redis, MinIO and Qdrant adapters on the Compose network.
"""

from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import time
import uuid
from typing import Any

import httpx

BACKEND_ROOT = os.getenv('SELF_HOST_BACKEND_ROOT', '/app')
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)


def require_environment(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise RuntimeError(f'{name} is required')
    return value


def require_response(response: httpx.Response, operation: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise RuntimeError(f'{operation} returned HTTP {response.status_code}')
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f'{operation} response was not an object')
    return payload


def stop_exact_deletion_task(queue: Any, queue_key: str, task_token: str, task_key: str) -> bool:
    """Stop this disposable retry before deleting its authority row.

    A failed delivery is briefly ``pending`` while the worker transitions it
    back to ``ready``. Wait for that active delivery, then remove only this
    smoke's queue structures. A stuck delivery returns False so cleanup keeps
    its marker/receipt rather than manufacturing a missing-authority retry.
    """
    pending_key = f'{queue_key}:pending'
    ready_key = f'{queue_key}:ready'
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and queue.zscore(pending_key, task_token) is not None:
        time.sleep(0.1)
    pending_drained = queue.zscore(pending_key, task_token) is None
    with queue.pipeline(transaction=True) as pipe:
        pipe.zrem(ready_key, task_token)
        pipe.zrem(pending_key, task_token)
        pipe.delete(task_key)
        pipe.execute()
    return pending_drained


def main() -> int:
    if os.getenv('AUTH_PROVIDER', '').strip().lower() != 'better_auth':
        raise RuntimeError('live acceptance requires AUTH_PROVIDER=better_auth')
    if os.getenv('STORAGE_BACKEND', '').strip().lower() != 'minio':
        raise RuntimeError('live acceptance requires STORAGE_BACKEND=minio')
    if os.getenv('VECTOR_STORE_PROVIDER', '').strip().lower() != 'qdrant':
        raise RuntimeError('live acceptance requires VECTOR_STORE_PROVIDER=qdrant')
    if os.getenv('QUEUE_BACKEND', '').strip().lower() != 'redis':
        raise RuntimeError('live acceptance requires QUEUE_BACKEND=redis')
    if os.getenv('STT_SERVICE_MODELS', '').strip().lower() != 'sensevoice':
        raise RuntimeError('live acceptance requires STT_SERVICE_MODELS=sensevoice')
    require_environment('FIRESTORE_PG_DSN')

    auth_url = require_environment('AUTH_SERVER_INTERNAL_URL').rstrip('/')
    # The production client URL stays explicit HTTPS, but an acceptance
    # container must not require public-DNS hairpinning to reach its sibling.
    backend_url = os.getenv('SELF_HOST_BACKEND_INTERNAL_URL', 'http://backend:8080').rstrip('/')
    auth_origin = require_environment('SELF_HOST_AUTH_ORIGIN')
    uid = ''
    marker_ref = None
    receipt_ref = None
    completed = False

    from database import users as users_db
    from database import vector_db
    from database._client import db
    from database.account_deletion_policy import account_deletion_receipt_id
    from utils import identity
    from utils.cloud_tasks_redis import _queue_names, _r, _task_key, _task_token
    from utils.llm.clients import embeddings, get_llm
    from utils.other import storage
    from utils.sensevoice.socket import sensevoice_model_is_ready

    if importlib.util.find_spec('sherpa_onnx') is None:
        raise RuntimeError('live acceptance backend image is missing the locked sherpa-onnx runtime')
    if not sensevoice_model_is_ready():
        raise RuntimeError('live acceptance SenseVoice mount is missing model.int8.onnx or tokens.txt')
    decode_program = """
import json
from utils.sensevoice.socket import _decode_pcm, get_sensevoice_recognizer

sample_rate = 16000
pcm = b'\\x00\\x00' * (sample_rate // 4)
text = _decode_pcm(get_sensevoice_recognizer(), sample_rate, pcm)
print(json.dumps({'pcm_milliseconds': 250, 'result_text_characters': len(text)}))
"""
    try:
        decode_process = subprocess.run(
            [sys.executable, '-c', decode_program],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('SenseVoice recognizer/decode exceeded the 120 second live-acceptance bound') from error
    if decode_process.returncode != 0:
        raise RuntimeError(f'SenseVoice recognizer/decode failed with exit code {decode_process.returncode}')
    try:
        decode_evidence = json.loads(decode_process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise RuntimeError('SenseVoice recognizer/decode returned no machine-readable result') from error
    if decode_evidence.get('pcm_milliseconds') != 250 or not isinstance(
        decode_evidence.get('result_text_characters'), int
    ):
        raise RuntimeError('SenseVoice recognizer/decode evidence is malformed')
    stt_readiness = {
        'selected': 'sensevoice',
        'sherpa_onnx_importable': True,
        'model_and_tokens_ready': True,
        'audio_decode_exercised': True,
        **decode_evidence,
    }
    model_reply = get_llm('chat_responses', request_timeout=30, max_retries=0).invoke(
        'Output exactly this token: neutral-route-ok'
    )
    model_text = str(getattr(model_reply, 'content', '')).strip()
    if 'neutral-route-ok' not in model_text.lower():
        raise RuntimeError('generic chat adapter did not return the acceptance marker')
    embedding = embeddings.embed_query('zero-vendor live replacement embedding')
    configured_dimension = int(require_environment('EMBEDDING_DIMENSION'))
    if len(embedding) != configured_dimension or not any(float(value) != 0.0 for value in embedding):
        raise RuntimeError('generic embedding adapter returned an invalid vector')
    generic_model_adapter = {
        'chat_response_marker_observed': True,
        'embedding_dimension': len(embedding),
        'embedding_nonzero': True,
    }

    try:
        email = f'self-host-live-{uuid.uuid4().hex}@example.invalid'
        password = f'SelfHost-{uuid.uuid4().hex}-Aa1!'
        with httpx.Client(timeout=20.0) as client:
            signup_response = client.post(
                f'{auth_url}/api/auth/sign-up/email',
                headers={'origin': auth_origin, 'x-forwarded-for': '192.0.2.2'},
                json={'name': 'Self Host Live Acceptance', 'email': email, 'password': password},
            )
            signup = require_response(signup_response, 'Better Auth sign-up')
            uid = str(signup.get('user', {}).get('id') or '')
            session_token = signup_response.headers.get('set-auth-token', '').strip()
            if not uid or not session_token:
                raise RuntimeError('Better Auth sign-up omitted user id or signed session token')
            marker_ref = db.collection('account_deletions').document(uid)
            receipt_ref = db.collection('account_deletion_receipts').document(account_deletion_receipt_id(uid))

            token_payload = require_response(
                client.get(
                    f'{auth_url}/api/auth/token',
                    headers={
                        'authorization': f'Bearer {session_token}',
                        'origin': auth_origin,
                        'x-forwarded-for': '192.0.2.2',
                    },
                ),
                'Better Auth JWT exchange',
            )
            jwt = str(token_payload.get('token') or '')
            claims = identity.verify_id_token(jwt)
            if claims.get('uid') != uid:
                raise RuntimeError('backend identity verifier returned the wrong uid')

            user_ref = db.collection('users').document(uid)
            user_ref.set({'email': email, 'name': 'Self Host Live Acceptance'})
            user_ref.collection('conversations').document('live-conversation').set({'uid': uid, 'status': 'processed'})
            db.collection('self_host_live_rows').document(f'owned-{uuid.uuid4().hex}').set(
                {'uid': uid, 'kind': 'acceptance'}
            )

            object_seeds = (
                (storage.memories_recordings_bucket, f'{uid}/live-conversation/audio.wav'),
                (storage.private_cloud_sync_bucket, f'chunks/{uid}/live-conversation/0.opus'),
            )
            object_client = storage.get_storage_client()
            for bucket_name, object_name in object_seeds:
                if not bucket_name:
                    raise RuntimeError('self-host object bucket configuration is incomplete')
                object_client.bucket(bucket_name).blob(object_name).upload_from_string(b'live-acceptance')

            if vector_db.index is None:
                raise RuntimeError('Qdrant vector adapter is not initialized')
            dimension = int(require_environment('EMBEDDING_DIMENSION'))
            vector_db.index.upsert(
                namespace='ns1',
                vectors=[
                    {
                        'id': f'{uid}-live-conversation',
                        'values': [0.01] * dimension,
                        'metadata': {'uid': uid, 'memory_id': 'live-conversation'},
                    }
                ],
            )

            seeded = {
                'pg_user_owned_rows': users_db.count_user_owned_rows(uid),
                'minio_user_owned_objects': storage.count_user_owned_objects(uid),
                'qdrant_user_owned_vectors': vector_db.count_user_vectors(uid),
                'auth': identity.account_residual_counts(uid),
            }
            if seeded['pg_user_owned_rows'] < 3:
                raise RuntimeError(f'PostgreSQL seed did not persist every owned row: {seeded}')
            if seeded['minio_user_owned_objects'] != len(object_seeds):
                raise RuntimeError(f'MinIO seed count mismatch: {seeded}')
            if seeded['qdrant_user_owned_vectors'] != 1:
                raise RuntimeError(f'Qdrant seed count mismatch: {seeded}')
            if seeded['auth']['users'] != 1 or seeded['auth']['sessions'] < 1 or seeded['auth']['accounts'] < 1:
                raise RuntimeError(f'Better Auth seed count mismatch: {seeded}')

            deletion = require_response(
                client.request(
                    'DELETE',
                    f'{backend_url}/v1/users/delete-account',
                    headers={'authorization': f'Bearer {jwt}'},
                    json={},
                ),
                'backend account deletion admission',
            )
            if deletion.get('status') != 'ok':
                raise RuntimeError('backend did not admit account deletion')

        deadline = time.monotonic() + 180
        marker: dict[str, Any] = {}
        receipt: dict[str, Any] = {}
        while time.monotonic() < deadline:
            marker_snapshot = marker_ref.get()
            receipt_snapshot = receipt_ref.get()
            marker = marker_snapshot.to_dict() or {}
            receipt = receipt_snapshot.to_dict() or {}
            status = marker.get('wipe_status')
            if not marker_snapshot.exists and receipt_snapshot.exists and receipt.get('wipe_status') == 'completed':
                completed = True
                break
            if status in {'failed', 'billing_failed', 'agent_vm_failed'}:
                raise RuntimeError(f'account deletion reached failure state {status}')
            time.sleep(1)
        if not completed:
            raise RuntimeError(
                'account deletion did not complete before timeout; '
                f'active_state={marker.get("wipe_status")}, receipt_state={receipt.get("wipe_status")}'
            )

        allowed_receipt_fields = {'schema_version', 'wipe_status', 'wipe_job_id', 'wipe_completed_at'}
        if set(receipt) != allowed_receipt_fields:
            raise RuntimeError(f'completed deletion receipt schema drifted: {sorted(receipt)}')
        receipt_serialized = repr(receipt)
        if uid in receipt_serialized or {'uid', 'reason', 'reason_details'} & set(receipt):
            raise RuntimeError('completed deletion receipt retained user-identifying or feedback content')

        wipe_job_id = str(receipt.get('wipe_job_id') or '')
        if not wipe_job_id:
            raise RuntimeError('completed deletion receipt omitted wipe_job_id')
        queue_key = _queue_names()['account-deletion']
        task_token = _task_token(f'wipe-{wipe_job_id}')
        task_key = _task_key(queue_key, task_token)
        queue = _r()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and queue.exists(task_key):
            time.sleep(0.25)

        residual = {
            'pg_user_owned_rows': users_db.count_user_owned_rows(uid),
            'pg_uid_keyed_control_rows': 1 if marker_ref.get().exists else 0,
            'pg_opaque_receipt_rows': 1 if receipt_ref.get().exists else 0,
            'minio_user_owned_objects': storage.count_user_owned_objects(uid),
            'qdrant_user_owned_vectors': vector_db.count_user_vectors(uid),
            'auth': identity.account_residual_counts(uid),
            'redis_task_hashes': int(queue.exists(task_key)),
            'redis_task_ready_members': int(queue.zscore(f'{queue_key}:ready', task_token) is not None),
            'redis_task_pending_members': int(queue.zscore(f'{queue_key}:pending', task_token) is not None),
        }
        expected_zero = {key: value for key, value in residual.items() if key not in {'auth', 'pg_opaque_receipt_rows'}}
        if any(value != 0 for value in expected_zero.values()) or any(
            value != 0 for value in residual['auth'].values()
        ):
            raise RuntimeError(f'live replacement reconciliation left residual data: {residual}')
        if residual['pg_opaque_receipt_rows'] != 1:
            raise RuntimeError('opaque completed-deletion receipt is missing')

        print(
            json.dumps(
                {
                    'status': 'passed',
                    'stt_readiness': stt_readiness,
                    'generic_model_adapter': generic_model_adapter,
                    'seeded': seeded,
                    'residual': residual,
                    'note': (
                        'the sole nonzero control row is an HMAC-keyed minimal receipt; '
                        'the UID-keyed marker and deletion feedback are absent'
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        # The acceptance account is disposable. This cleanup runs after the
        # steady-state reconciliation snapshot above and also limits debris on
        # a failed smoke; failures here must not hide the original assertion.
        control_cleanup_safe = True
        wipe_job_id = ''
        for control_ref in (marker_ref, receipt_ref):
            if control_ref is None:
                continue
            try:
                wipe_job_id = str((control_ref.get().to_dict() or {}).get('wipe_job_id') or wipe_job_id)
            except Exception:
                pass
        if wipe_job_id:
            try:
                cleanup_queue_key = _queue_names()['account-deletion']
                cleanup_task_token = _task_token(f'wipe-{wipe_job_id}')
                control_cleanup_safe = stop_exact_deletion_task(
                    _r(),
                    cleanup_queue_key,
                    cleanup_task_token,
                    _task_key(cleanup_queue_key, cleanup_task_token),
                )
            except Exception:
                control_cleanup_safe = False
        if uid:
            for cleanup in (
                lambda: vector_db.delete_all_user_vectors(uid),
                lambda: storage.delete_all_user_owned_objects(uid),
                lambda: users_db.delete_user_data(uid),
                lambda: identity.delete_user(uid),
            ):
                try:
                    cleanup()
                except Exception:
                    pass
        if control_cleanup_safe and marker_ref is not None:
            try:
                marker_ref.delete()
            except Exception:
                pass
        if control_cleanup_safe and receipt_ref is not None:
            try:
                receipt_ref.delete()
            except Exception:
                pass


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'ERROR: live replacement smoke failed: {error}', file=sys.stderr)
        raise SystemExit(1)
