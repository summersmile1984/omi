#!/usr/bin/env python3
"""Run one assembled self-host product loop through the local HTTPS edge.

This is deliberately not an adapter probe: identity, Capture, Understand,
Remember, Retrieve and Act use the public HTTP/WSS routes.  Only the
authoritative evidence reads and the scheduled canonical-memory maintenance
tick execute inside the production backend image.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import json
import os
import re
import ssl
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import Any

import httpx
import websockets

BACKEND_ROOT = os.getenv('SELF_HOST_BACKEND_ROOT', '/app')
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)


def require_environment(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise RuntimeError(f'{name} is required')
    return value


def require_object(response: httpx.Response, operation: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise RuntimeError(f'{operation} returned HTTP {response.status_code}')
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f'{operation} response was not an object')
    return payload


def jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split('.')
    if len(parts) != 3:
        raise RuntimeError('Better Auth token is not a compact JWT')
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
    except (ValueError, json.JSONDecodeError) as error:
        raise RuntimeError('Better Auth JWT payload is malformed') from error
    if not isinstance(payload, dict):
        raise RuntimeError('Better Auth JWT payload is not an object')
    return payload


def jwt_kid(token: str) -> str:
    part = token.split('.')[0]
    try:
        header = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
    except (ValueError, json.JSONDecodeError) as error:
        raise RuntimeError('Better Auth JWT header is malformed') from error
    return str(header.get('kid') or '') if isinstance(header, dict) else ''


def normalized_words(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', value.lower()).strip()


def transcript_text(payload: dict[str, Any]) -> str:
    segments = payload.get('transcript_segments') or []
    if not isinstance(segments, list):
        return ''
    return ' '.join(str(segment.get('text') or '') for segment in segments if isinstance(segment, dict)).strip()


def parse_ws_event(raw: str | bytes) -> Any:
    """Parse a product event while ignoring the listen wire heartbeat."""

    if not isinstance(raw, str) or raw == 'ping':
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError('public WSS emitted a malformed non-heartbeat text frame') from error


def public_agent_turn(
    client: httpx.Client,
    *,
    backend_url: str,
    headers: dict[str, str],
    prompt: str,
) -> tuple[str, list[str]]:
    """Execute an authenticated agent turn and return its public SSE evidence."""

    thoughts: list[str] = []
    done_payload: dict[str, Any] | None = None
    with client.stream(
        'POST',
        f'{backend_url}/v2/messages',
        headers=headers,
        json={'text': prompt, 'file_ids': []},
        timeout=120,
    ) as response:
        if response.status_code >= 400:
            raise RuntimeError(f'public Understand agent returned HTTP {response.status_code}')
        for line in response.iter_lines():
            normalized = line.replace('__CRLF__', '\n').strip()
            if normalized.startswith('think:'):
                thoughts.append(normalized.removeprefix('think:').strip())
            if normalized.startswith('done: '):
                try:
                    decoded = base64.b64decode(normalized.removeprefix('done: ').strip())
                    candidate = json.loads(decoded)
                except (ValueError, json.JSONDecodeError) as error:
                    raise RuntimeError('public Understand terminal SSE frame is malformed') from error
                if isinstance(candidate, dict):
                    done_payload = candidate
    answer = str((done_payload or {}).get('text') or '').strip()
    if not answer:
        raise RuntimeError('public agent returned no terminal answer')
    return answer, thoughts


def public_understand(
    client: httpx.Client,
    *,
    backend_url: str,
    headers: dict[str, str],
    transcript: str,
    expected_transcript: str,
    marker: str,
) -> str:
    required_terms = sorted({term for term in normalized_words(expected_transcript).split() if len(term) >= 7})[:3]
    answer, _ = public_agent_turn(
        client,
        backend_url=backend_url,
        headers=headers,
        prompt=(
            'Understand the captured transcript below and summarize it in one sentence. '
            f'Include the exact marker {marker} and repeat these transcript words exactly: '
            f'{", ".join(required_terms)}. Transcript: {transcript}'
        ),
    )
    if marker not in answer:
        raise RuntimeError('public Understand response did not preserve the per-run marker')
    expected_terms = set(required_terms)
    observed_terms = set(normalized_words(answer).split())
    matched_terms = expected_terms & observed_terms
    if len(matched_terms) < 2:
        raise RuntimeError('public Understand response did not preserve two distinctive captured-transcript terms')
    return answer


def public_agent_web_search(
    client: httpx.Client,
    *,
    backend_url: str,
    headers: dict[str, str],
    marker: str,
) -> dict[str, Any]:
    """Require a separate authenticated product-agent turn to use SearXNG."""

    answer, thoughts = public_agent_turn(
        client,
        backend_url=backend_url,
        headers=headers,
        prompt=(
            'Search the public web for "Wizard of Oz". Your first action MUST be to call '
            'web_search_tool exactly once with that exact query; do not answer from memory. '
            f'After the tool result, include the exact marker {marker} and the first source URL.'
        ),
    )
    source_match = re.search(r'https?://[^\s)\]}]+wikipedia\.org[^\s)\]}]*', answer, flags=re.IGNORECASE)
    if not any('Searching the web' in thought for thought in thoughts):
        raise RuntimeError('public product agent did not emit a web-search tool execution event')
    if source_match is None:
        raise RuntimeError('public product agent response did not return the SearXNG Wikipedia source')
    return {'text': answer, 'source_url': source_match.group(0), 'web_search_tool_event': True}


async def capture_over_public_wss(
    *,
    backend_url: str,
    origin: str,
    token: str,
    ca_file: str,
    wav_path: Path,
    conversation_id: str,
) -> dict[str, Any]:
    context = ssl.create_default_context(cafile=ca_file)
    url = (
        backend_url.replace('https://', 'wss://', 1)
        + '/v4/web/listen?sample_rate=16000&codec=pcm16&channels=1'
        + '&include_speech_profile=false&create_speakers=false&source=cutover-acceptance'
        + f'&client_conversation_id={conversation_id}'
    )
    async with websockets.connect(url, ssl=context, origin=origin, open_timeout=20, close_timeout=10) as socket:
        await socket.send(json.dumps({'type': 'auth', 'token': token}))
        events: list[Any] = []
        ready = False
        session = None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and (not ready or session is None):
            raw = await asyncio.wait_for(socket.recv(), timeout=max(1, deadline - time.monotonic()))
            event = parse_ws_event(raw)
            events.append(event)
            if event == {'type': 'auth_response', 'success': False}:
                raise RuntimeError('public WSS rejected the Better Auth JWT')
            if isinstance(event, dict) and event.get('type') == 'conversation_session':
                session = event
            if isinstance(event, dict) and event.get('type') == 'service_status' and event.get('status') == 'ready':
                ready = True
        if not ready or not isinstance(session, dict):
            raise RuntimeError('public WSS did not reach a ready conversation session')
        if session.get('conversation_id') != conversation_id:
            raise RuntimeError('public WSS changed the client conversation identity')

        with wave.open(str(wav_path), 'rb') as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
                raise RuntimeError('capture fixture must be mono PCM16 at 16kHz')
            pcm = source.readframes(source.getnframes())
        # SenseVoice emits bounded five-second windows. The checked-in fixture
        # is 4.905s, so append silence instead of modifying the release corpus.
        pcm += b'\x00\x00' * 3200
        for offset in range(0, len(pcm), 3200):
            await socket.send(pcm[offset : offset + 3200])
            await asyncio.sleep(0.005)

        emitted_segments: list[dict[str, Any]] = []
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not emitted_segments:
            raw = await asyncio.wait_for(socket.recv(), timeout=max(1, deadline - time.monotonic()))
            event = parse_ws_event(raw)
            events.append(event)
            if isinstance(event, list):
                emitted_segments = [item for item in event if isinstance(item, dict) and item.get('text')]
        if not emitted_segments:
            raise RuntimeError('SenseVoice public WSS emitted no transcript segments')
        return {
            'conversation_id': conversation_id,
            'segment_ids': [str(item.get('id') or '') for item in emitted_segments],
            'emitted_text': ' '.join(str(item.get('text') or '') for item in emitted_segments).strip(),
            'ready_provider': next(
                (
                    str(item.get('provider') or item.get('model') or 'sensevoice')
                    for item in events
                    if isinstance(item, dict) and item.get('type') == 'service_status' and item.get('status') == 'ready'
                ),
                'sensevoice',
            ),
        }


def main() -> int:
    backend_url = require_environment('PUBLIC_BACKEND_URL').rstrip('/')
    auth_url = require_environment('PUBLIC_AUTH_URL').rstrip('/')
    mcp_url = require_environment('PUBLIC_MCP_URL').rstrip('/')
    origin = require_environment('SELF_HOST_AUTH_ORIGIN')
    ca_file = require_environment('SELF_HOST_CUTOVER_CA_FILE')
    live_egress_evidence = json.loads(require_environment('SELF_HOST_LIVE_EGRESS_EVIDENCE_JSON'))
    searxng_settings_evidence = json.loads(require_environment('SELF_HOST_SEARXNG_SETTINGS_EVIDENCE_JSON'))
    if not isinstance(live_egress_evidence, dict) or not isinstance(searxng_settings_evidence, dict):
        raise RuntimeError('live policy evidence must be JSON objects')
    if (
        searxng_settings_evidence.get('effective_secret_nonempty') is not True
        or searxng_settings_evidence.get('effective_secret_not_known_default') is not True
        or searxng_settings_evidence.get('effective_secret_matches_configured') is not True
    ):
        raise RuntimeError('SearXNG runtime secret evidence is missing or unsafe')
    wav_path = Path(require_environment('SELF_HOST_CAPTURE_WAV'))
    manifest_path = Path(require_environment('SELF_HOST_CAPTURE_MANIFEST'))
    if os.getenv('SPEAKER_EMBEDDING_PROVIDER') != 'disabled':
        raise RuntimeError('this acceptance profile expects the explicitly disabled speaker-identity capability')
    if os.getenv('WEB_SEARCH_TRANSPORT') != 'searxng':
        raise RuntimeError('assembled live loop requires the SearXNG production transport')

    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    expected_text = str(manifest.get('expected_transcript') or manifest.get('expected_text') or '')
    if not expected_text:
        raise RuntimeError('capture manifest omitted expected transcript')
    marker = f'cutover-{uuid.uuid4().hex[:12]}'
    conversation_id = str(uuid.uuid4())
    uid = ''

    from database import users as users_db
    from database import vector_db
    from database._client import db
    from langchain_core.output_parsers import PydanticOutputParser
    from utils import identity
    from utils.llm.clients import get_llm
    from utils.memory.canonical_consolidation import ConsolidationAgentBatch
    from utils.memory.canonical_required_processing import invoke_required_memory_processor
    from utils.memory.short_term_promotion import run_canonical_short_term_maintenance
    from utils.other import storage

    global_gate_ref = db.document('memory_control/global_read_gate')
    global_gate_before = global_gate_ref.get()
    global_gate_before_payload = global_gate_before.to_dict() if global_gate_before.exists else None
    seeded_global_gate = False

    def cleanup_fixture() -> None:
        if seeded_global_gate:
            try:
                if isinstance(global_gate_before_payload, dict):
                    global_gate_ref.set(global_gate_before_payload)
                else:
                    global_gate_ref.delete()
            except Exception:
                pass
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

    atexit.register(cleanup_fixture)

    verify = ssl.create_default_context(cafile=ca_file)
    with httpx.Client(timeout=60, verify=verify, trust_env=False) as client:
        health = require_object(client.get(f'{backend_url}/v1/health'), 'public backend health')
        if not health:
            raise RuntimeError('public backend health returned no evidence')

        email = f'assembled-{uuid.uuid4().hex}@example.invalid'
        password = f'Assembled-{uuid.uuid4().hex}-Aa1!'
        signup_response = client.post(
            f'{auth_url}/api/auth/sign-up/email',
            headers={'origin': origin, 'x-forwarded-for': '192.0.2.44'},
            json={'name': 'Assembled Cutover Acceptance', 'email': email, 'password': password},
        )
        signup = require_object(signup_response, 'public Better Auth sign-up')
        uid = str(signup.get('user', {}).get('id') or '')
        session_token = signup_response.headers.get('set-auth-token', '').strip()
        if not uid or not session_token:
            raise RuntimeError('public Better Auth signup omitted uid or session token')
        token = str(
            require_object(
                client.get(
                    f'{auth_url}/api/auth/token',
                    headers={
                        'authorization': f'Bearer {session_token}',
                        'origin': origin,
                        'x-forwarded-for': '192.0.2.44',
                    },
                ),
                'public Better Auth JWT exchange',
            ).get('token')
            or ''
        )
        claims = jwt_payload(token)
        token_subject = str(claims.get('uid') or claims.get('sub') or '')
        if token_subject != uid or claims.get('iss') != auth_url or claims.get('aud') != auth_url:
            raise RuntimeError(
                'public JWT uid/issuer/audience does not match the configured HTTPS Auth origin: '
                f'subject_match={token_subject == uid}, iss={claims.get("iss")!r}, '
                f'aud={claims.get("aud")!r}, expected_origin={auth_url!r}'
            )
        verified = identity.verify_id_token(token)
        if verified.get('uid') != uid:
            raise RuntimeError('backend private-JWKS verifier rejected the public HTTPS-issued token')
        jwks = require_object(client.get(f'{auth_url}/api/auth/jwks'), 'public Better Auth JWKS')
        kid = jwt_kid(token)
        if not kid or kid not in {str(key.get('kid') or '') for key in jwks.get('keys', []) if isinstance(key, dict)}:
            raise RuntimeError('public JWKS does not retain the JWT signing key')
        blocked = client.get(f'{auth_url}/internal/users/{uid}')
        if blocked.status_code != 404:
            raise RuntimeError('public Auth origin exposed the private lifecycle API')

        authorization_metadata = require_object(
            client.get(f'{mcp_url}/.well-known/oauth-authorization-server'), 'public MCP authorization metadata'
        )
        resource_metadata = require_object(
            client.get(f'{mcp_url}/.well-known/oauth-protected-resource/v1/mcp/sse'),
            'public MCP protected-resource metadata',
        )
        if authorization_metadata.get('issuer') != mcp_url:
            raise RuntimeError('MCP issuer does not match PUBLIC_MCP_URL')
        expected_resource = f'{mcp_url}/v1/mcp/sse'
        if resource_metadata.get('resource') != expected_resource or resource_metadata.get('authorization_servers') != [
            mcp_url
        ]:
            raise RuntimeError('MCP protected-resource metadata does not match the public HTTPS origin')

        auth_headers = {'authorization': f'Bearer {token}'}
        profile = require_object(
            client.get(
                f'{backend_url}/v1/users/profile',
                headers={**auth_headers, 'x-app-platform': 'web'},
            ),
            'public backend user provisioning/profile',
        )
        if profile.get('uid') != uid:
            raise RuntimeError('public backend profile did not provision the Better Auth principal')
        capture = asyncio.run(
            capture_over_public_wss(
                backend_url=backend_url,
                origin=origin,
                token=token,
                ca_file=ca_file,
                wav_path=wav_path,
                conversation_id=conversation_id,
            )
        )
        deadline = time.monotonic() + 30
        conversation: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = client.get(f'{backend_url}/v1/conversations/{conversation_id}', headers=auth_headers)
            if response.status_code == 200:
                conversation = require_object(response, 'captured conversation readback')
                if transcript_text(conversation):
                    break
            time.sleep(0.5)
        persisted_transcript = transcript_text(conversation)
        observed = normalized_words(f"{capture['emitted_text']} {persisted_transcript}")
        expected = normalized_words(expected_text)
        if expected not in observed:
            raise RuntimeError('captured transcript did not match the checked-in audio fixture manifest')

        understood_text = public_understand(
            client,
            backend_url=backend_url,
            headers=auth_headers,
            transcript=persisted_transcript,
            expected_transcript=expected_text,
            marker=marker,
        )
        web_search = public_agent_web_search(
            client,
            backend_url=backend_url,
            headers=auth_headers,
            marker=marker,
        )
        # The per-run marker reached this point through the authenticated
        # Understand turn above. Keep the durable proposition concise so the
        # canonical subject/predicate parser and the L2 planner receive the
        # same unambiguous primary-user preference.
        memory_content = f'I prefer to drink the tea blend named {marker} every morning.'
        remembered = require_object(
            client.post(
                f'{backend_url}/v3/memories',
                headers=auth_headers,
                json={'content': memory_content, 'category': 'manual'},
            ),
            'public Remember write',
        )
        memory_id = str(remembered.get('id') or remembered.get('memory_id') or '')
        if not memory_id:
            raise RuntimeError('Remember response omitted the canonical memory identity')

        required_processing_llm = get_llm('memory_l2')
        consolidation_llm = get_llm('memory_conflict')
        consolidation_decision_summary: list[dict[str, Any]] = []

        def invoke_live_consolidation(messages: Any) -> str:
            response = consolidation_llm.invoke(messages)
            content = str(getattr(response, 'content', response) or '')
            try:
                batch = PydanticOutputParser(pydantic_object=ConsolidationAgentBatch).parse(content)
            except Exception:
                consolidation_decision_summary.append({'parseable': False})
                return content
            consolidation_decision_summary.extend(
                {
                    'parseable': True,
                    'route': decision.route,
                    'subject_is_primary_user': decision.subject_entity_id == 'user',
                    'relationship_to_user': decision.relationship_to_user,
                    'aboutness': decision.aboutness,
                    'basis_for_memory': decision.basis_for_memory,
                }
                for decision in batch.decisions
            )
            return content

        maintenance = run_canonical_short_term_maintenance(
            uid,
            db_client=db,
            run_id=f'cutover-{marker}',
            required_processor=lambda item: invoke_required_memory_processor(item, required_processing_llm),
            required_processing_limit=5,
            llm_invoke=invoke_live_consolidation,
        )
        if (
            maintenance.required_processing is None
            or memory_id not in maintenance.required_processing.processed_memory_ids
        ):
            report = maintenance.required_processing
            raise RuntimeError(
                'canonical maintenance did not process the remembered item: ' f'{getattr(report, "__dict__", None)!r}'
            )
        item_snapshot = db.document(f'users/{uid}/memory_items/{memory_id}').get()
        item = item_snapshot.to_dict() or {}
        commit_id = str(item.get('ledger_commit_id') or '')
        if item.get('processing_state') != 'processed' or not commit_id:
            raise RuntimeError('Remember item did not reach processed durable canonical state')
        long_term_admission = (
            'passed'
            if item.get('tier') == 'long_term' and (item.get('promotion') or {}).get('route') == 'promote'
            else 'retry_pending'
        )

        current_global_gate = global_gate_ref.get()
        current_global_payload = current_global_gate.to_dict() or {}
        if (
            current_global_payload.get('memory_reads_enabled') is not True
            or current_global_payload.get('kill_switch_active') is True
        ):
            if os.getenv('SELF_HOST_ACCEPTANCE_ALLOW_CONTROL_SEED', '').lower() != 'true':
                raise RuntimeError('external cutover target has not enabled the global memory read gate')
            global_gate_ref.set({'memory_reads_enabled': True, 'kill_switch_active': False})
            seeded_global_gate = True
        db.document(f'users/{uid}/memory_control/state').set(
            {
                'schema_version': 1,
                'uid': uid,
                'mode': 'read',
                'fallback_projection_ready': True,
                'vector_projection_commit_id': commit_id,
                'account_generation': int(item.get('account_generation') or 1),
                'stage_gates': {'shadow': 'passed', 'write': 'passed', 'read': 'passed'},
                'grants': {'omi_chat': {'default_memory': True}},
            },
            merge=True,
        )
        retrieved = require_object(
            client.get(
                f'{backend_url}/memory/vector/search',
                headers=auth_headers,
                params={'query': marker, 'limit': 10},
            ),
            'public Retrieve vector search',
        )
        retrieved_items = retrieved.get('items') or []
        matched = next(
            (
                row
                for row in retrieved_items
                if isinstance(row, dict)
                and str(row.get('memory_id') or '') == memory_id
                and marker in str(row.get('content') or '')
            ),
            None,
        )
        if matched is None:
            raise RuntimeError(
                'public Retrieve did not hydrate the remembered item from Qdrant candidates: '
                f'outbox={maintenance.outbox!r}, item_state={item.get("processing_state")!r}, '
                f'tier={item.get("tier")!r}, status={item.get("status")!r}, '
                f'source_state={item.get("source_state")!r}, '
                f'promotion_route={(item.get("promotion") or {}).get("route")!r}, '
                f'commit_present={bool(commit_id)}, '
                f'returned_ids={[row.get("memory_id") for row in retrieved_items if isinstance(row, dict)]!r}'
            )

        excerpt_hash = hashlib.sha256(persisted_transcript.encode('utf-8')).hexdigest()
        action = require_object(
            client.post(
                f'{backend_url}/v1/action-items',
                headers=auth_headers,
                json={
                    'description': f'Act on retrieved self-host memory {marker}',
                    'source': 'cutover_acceptance',
                    'conversation_id': conversation_id,
                    'provenance': [
                        {
                            'kind': 'conversation',
                            'id': conversation_id,
                            'scope': 'canonical',
                            'excerpt_hash': excerpt_hash,
                            'transcript_segment_ids': capture['segment_ids'],
                        },
                        {
                            'kind': 'memory_item',
                            'id': memory_id,
                            'version': str(item.get('version') or 1),
                            'scope': 'canonical',
                        },
                    ],
                },
            ),
            'public Act write',
        )
        action_id = str(action.get('id') or '')
        action_readback = require_object(
            client.get(f'{backend_url}/v1/action-items/{action_id}', headers=auth_headers), 'public Act readback'
        )
        if (
            marker not in str(action_readback.get('description') or '')
            or len(action_readback.get('provenance') or []) != 2
        ):
            raise RuntimeError('Act readback lost the retrieved-memory/conversation provenance chain')

        result = {
            'status': 'passed',
            'https_origin_and_hairpin': {
                'public_backend_url': backend_url,
                'public_auth_url': auth_url,
                'public_mcp_url': mcp_url,
                'temporary_ca_verified': True,
                'jwt_issuer_audience_exact': True,
                'public_jwks_kid_present': True,
                'backend_private_jwks_verification': True,
                'auth_private_lifecycle_blocked_at_edge': True,
                'mcp_metadata_exact': True,
                'backend_principal_provisioned_via_public_profile': True,
                'wss_public_origin_exercised': True,
            },
            'live_egress': {
                **live_egress_evidence,
                'dns_denial_claimed': False,
                'searxng_declared_outbound_engine_allowlist': ['wikipedia'],
            },
            'assembled_product_loop': {
                'capture': {
                    'conversation_id': conversation_id,
                    'fixture_manifest_match': True,
                    'persisted_transcript_characters': len(persisted_transcript),
                    'speaker_identity_capability': 'disabled_not_exercised',
                    'speaker_identity_functional_equivalence_claimed': False,
                },
                'understand': {'generic_model_response_marker': True},
                'web_search': {
                    'transport': 'searxng',
                    'public_product_agent_route_exercised': True,
                    'sse_tool_execution_event_observed': web_search['web_search_tool_event'],
                    'outbound_engine_allowlist': ['wikipedia'],
                    'returned_source_host': 'wikipedia.org',
                    'effective_secret_nonempty': True,
                    'effective_secret_not_known_default': True,
                    'effective_secret_matches_configured': True,
                    'direct_adapter_probe_used_as_loop_evidence': False,
                },
                'remember': {
                    'memory_id': memory_id,
                    'pg_processing_state': item.get('processing_state'),
                    'ledger_commit_id_present': True,
                    'qdrant_projection_delivered': True,
                    'long_term_admission': long_term_admission,
                    'long_term_admission_errors': list(getattr(maintenance.consolidation, 'errors', []) or []),
                    'long_term_decision_shape': consolidation_decision_summary,
                },
                'retrieve': {'public_vector_search_hydrated_memory_id': memory_id},
                'act': {
                    'action_item_id': action_id,
                    'canonical_conversation_and_memory_provenance': True,
                },
            },
        }
        print(json.dumps(result, sort_keys=True))

    # The separate live-replacement smoke owns asynchronous deletion and
    # reconciliation evidence; this fixture removes its disposable chain on
    # success and through atexit on every failure path.
    cleanup_fixture()
    atexit.unregister(cleanup_fixture)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'ERROR: assembled cutover smoke failed: {error}', file=sys.stderr)
        raise SystemExit(1)
