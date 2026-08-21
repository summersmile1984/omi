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
import io
import json
import math
import os
import re
import ssl
import struct
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
import websockets

from public_object_evidence import public_signed_object_crud

BACKEND_ROOT = os.getenv('SELF_HOST_BACKEND_ROOT', '/app')
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)


def require_environment(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise RuntimeError(f'{name} is required')
    return value


def mounted_model_artifact_identity() -> dict[str, Any]:
    artifacts = {
        'sensevoice_model': Path('/models/sensevoice/model.int8.onnx'),
        'sensevoice_tokens': Path('/models/sensevoice/tokens.txt'),
        'speaker_embedding_model': Path('/models/speaker/speaker.onnx'),
        'tts_model': Path('/models/tts/model.onnx'),
        'tts_tokens': Path('/models/tts/tokens.txt'),
    }
    evidence: dict[str, dict[str, Any]] = {}
    for name, path in artifacts.items():
        if not path.is_file():
            raise RuntimeError(f'mounted model artifact is missing: {name}')
        digest = hashlib.sha256()
        size = 0
        with path.open('rb') as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        if size <= 0:
            raise RuntimeError(f'mounted model artifact is empty: {name}')
        evidence[name] = {'sha256': digest.hexdigest(), 'bytes': size}
    return {'status': 'passed', 'paths_recorded': False, 'artifacts': evidence}


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


def _require_mlx_moss_model_catalog(payload: Any, configured_model: str) -> list[str]:
    if not isinstance(payload, dict) or payload.get('object') != 'list' or not isinstance(payload.get('data'), list):
        raise RuntimeError('mlx-audio /v1/models response did not use the reviewed OpenAI list wire')
    model_ids = [
        str(item.get('id') or '')
        for item in payload['data']
        if isinstance(item, dict) and item.get('object') == 'model' and item.get('id')
    ]
    if configured_model not in model_ids:
        raise RuntimeError('mlx-audio /v1/models did not report the exact configured diarization model id')
    return model_ids


def _summarize_mlx_moss_segments(segments: Any, *, audio_duration_seconds: float) -> dict[str, Any]:
    if not isinstance(segments, list) or not segments:
        raise RuntimeError('mlx-audio MOSS diarization returned no transcript segments')
    speakers: list[str] = []
    last_speaker = ''
    transitions = 0
    last_end = 0.0
    for segment in segments:
        if not isinstance(segment, dict):
            raise RuntimeError('mlx-audio MOSS diarization returned a non-object segment')
        timestamp = segment.get('timestamp')
        speaker = str(segment.get('speaker') or '')
        text = str(segment.get('text') or '').strip()
        if (
            not isinstance(timestamp, (list, tuple))
            or len(timestamp) != 2
            or not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in timestamp)
        ):
            raise RuntimeError('mlx-audio MOSS diarization returned an invalid segment timestamp')
        start, end = float(timestamp[0]), float(timestamp[1])
        if start < 0 or end <= start or start < last_end - 0.25:
            raise RuntimeError('mlx-audio MOSS diarization returned incoherent segment timing')
        if not re.fullmatch(r'SPEAKER_\d+', speaker) or not text:
            raise RuntimeError('mlx-audio MOSS diarization returned invalid speaker/text evidence')
        if last_speaker and speaker != last_speaker:
            transitions += 1
        if speaker not in speakers:
            speakers.append(speaker)
        last_speaker = speaker
        last_end = end
    if len(speakers) < 2:
        raise RuntimeError('mlx-audio MOSS diarization did not identify at least two speakers')
    if transitions < 2:
        raise RuntimeError('mlx-audio MOSS diarization did not produce multiple speaker transitions')
    if last_end > audio_duration_seconds + 1.0:
        raise RuntimeError('mlx-audio MOSS segment timing exceeded the WAV duration')
    return {
        'segment_count': len(segments),
        'speaker_ids': speakers,
        'speaker_count': len(speakers),
        'speaker_transition_count': transitions,
        'last_segment_end_seconds': round(last_end, 3),
    }


def probe_mlx_moss_diarization(wav_path: Path) -> dict[str, Any]:
    """Exercise the selected production adapter against the operator service."""

    selector = require_environment('STT_PRERECORDED_MODEL')
    if selector != 'mlx_moss_diarize':
        raise RuntimeError('assembled live loop requires STT_PRERECORDED_MODEL=mlx_moss_diarize')
    endpoint = require_environment('MLX_MOSS_DIARIZE_ENDPOINT')
    configured_model = require_environment('MLX_MOSS_DIARIZE_MODEL')
    api_key = os.getenv('MLX_MOSS_DIARIZE_API_KEY', '').strip()
    parsed = urlsplit(endpoint)
    if parsed.path != '/v1/audio/transcriptions' or parsed.query or parsed.fragment or parsed.username is not None:
        raise RuntimeError('mlx-audio MOSS endpoint did not use the exact reviewed transcription path')
    models_url = urlunsplit((parsed.scheme, parsed.netloc, '/v1/models', '', ''))
    headers = {'authorization': f'Bearer {api_key}'} if api_key else {}
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.get(models_url, headers=headers)
        catalog = require_object(response, 'mlx-audio model catalog')
    catalog_model_ids = _require_mlx_moss_model_catalog(catalog, configured_model)

    with wave.open(str(wav_path), 'rb') as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        frames = source.getnframes()
    if (channels, sample_width, sample_rate) != (1, 2, 16000) or frames <= 0:
        raise RuntimeError('mlx-audio MOSS acceptance fixture must be non-empty mono PCM16 at 16kHz')
    audio_duration_seconds = frames / sample_rate
    audio_bytes = wav_path.read_bytes()

    from utils.stt.pre_recorded import prerecorded_from_bytes

    segments = prerecorded_from_bytes(
        audio_bytes,
        sample_rate=sample_rate,
        channels=channels,
        diarize=True,
        language='multi',
        return_language=False,
    )
    segment_evidence = _summarize_mlx_moss_segments(segments, audio_duration_seconds=audio_duration_seconds)
    return {
        'status': 'passed',
        'provider': 'mlx_moss_diarize',
        'route': {
            'endpoint_origin': f'{parsed.scheme}://{parsed.netloc}',
            'transcription_path': parsed.path,
            'models_catalog_path': '/v1/models',
            'multipart_model': configured_model,
            'response_format': 'verbose_json',
            'authorization': 'bearer' if api_key else 'none',
        },
        'configured_model': configured_model,
        'model_catalog_exact_id_match': configured_model in catalog_model_ids,
        'real_transcription_route_exercised': True,
        'audio_sha256': hashlib.sha256(audio_bytes).hexdigest(),
        'audio_duration_seconds': round(audio_duration_seconds, 3),
        'audio_duration_source': 'wav_header_frames_divided_by_sample_rate',
        **segment_evidence,
        # mlx-audio exposes no model revision/cache provenance. Those remain
        # operator responsibilities and are intentionally not source-attested.
        'service_revision_reported': False,
        'operator_model_source_attested_by_gate': False,
    }


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
    file_ids: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Execute an authenticated agent turn and return its public SSE evidence."""

    thoughts: list[str] = []
    done_payload: dict[str, Any] | None = None
    with client.stream(
        'POST',
        f'{backend_url}/v2/messages',
        headers=headers,
        json={'text': prompt, 'file_ids': file_ids or []},
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


async def public_realtime_relay_roundtrip(
    *,
    client: httpx.Client,
    backend_url: str,
    origin: str,
    token: str,
    ca_file: str,
    marker: str,
) -> dict[str, Any]:
    mint = require_object(
        client.post(
            f'{backend_url}/v2/realtime/session',
            headers={'authorization': f'Bearer {token}'},
            json={'provider': 'relay'},
        ),
        'public realtime relay session mint',
    )
    if (
        mint.get('transport') != 'websocket_relay'
        or mint.get('protocol') != 'omi.realtime.v1'
        or mint.get('wire_protocol') != 'openai_realtime_v1'
    ):
        raise RuntimeError('public realtime mint did not select the reviewed relay wire contract')
    path = str(mint.get('websocket_url') or '')
    if not path.startswith('/'):
        raise RuntimeError('public realtime mint omitted its backend-relative WebSocket path')
    context = ssl.create_default_context(cafile=ca_file)
    event = {
        'type': 'session.update',
        'session': {'modalities': ['text']},
        'acceptance_marker': marker,
    }
    async with websockets.connect(
        backend_url.replace('https://', 'wss://', 1) + path,
        ssl=context,
        origin=origin,
        extra_headers={'Authorization': f'Bearer {token}'},
        subprotocols=['omi.realtime.v1'],
        open_timeout=20,
        close_timeout=10,
    ) as socket:
        if socket.subprotocol != 'omi.realtime.v1':
            raise RuntimeError('public realtime relay did not negotiate omi.realtime.v1')
        await socket.send(json.dumps(event, separators=(',', ':')))
        raw = await asyncio.wait_for(socket.recv(), timeout=20)
        try:
            response = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError('public realtime relay returned a malformed upstream event') from error
        if response != {'type': 'session.updated', 'acceptance_marker': marker}:
            raise RuntimeError('public realtime relay response did not preserve the client marker')
    return {
        'status': 'passed',
        'mint_route_selected': True,
        'wire_protocol': 'openai_realtime_v1',
        'client_event_forwarded': True,
        'upstream_response_forwarded': True,
    }


def public_local_tts(client: httpx.Client, *, backend_url: str, headers: dict[str, str], marker: str) -> dict[str, Any]:
    started = time.monotonic()
    response = client.post(
        f'{backend_url}/v2/tts/synthesize',
        headers=headers,
        json={'text': f'Local speech acceptance {marker}.', 'output_format': 'wav'},
        timeout=120,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if response.status_code != 200 or response.headers.get('content-type', '').split(';', 1)[0] != 'audio/wav':
        raise RuntimeError(f'public local TTS returned HTTP {response.status_code} or a non-WAV content type')
    audio = response.content
    if not audio.startswith(b'RIFF') or audio[8:12] != b'WAVE':
        raise RuntimeError('public local TTS returned invalid WAV bytes')
    try:
        with wave.open(io.BytesIO(audio), 'rb') as source:
            channels = source.getnchannels()
            rate = source.getframerate()
            frames = source.getnframes()
            width = source.getsampwidth()
            pcm = source.readframes(frames)
    except (EOFError, wave.Error) as error:
        raise RuntimeError('public local TTS WAV could not be decoded') from error
    if channels < 1 or rate < 8000 or frames < 1 or width not in {1, 2, 3, 4} or not any(pcm):
        raise RuntimeError('public local TTS WAV was empty, invalid, or silent')
    return {
        'status': 'passed',
        'provider': 'sherpa_onnx',
        'public_route_exercised': True,
        'wav_decode_exercised': True,
        'non_silent_pcm': True,
        'bytes': len(audio),
        'sample_rate': rate,
        'generation_duration_ms': elapsed_ms,
    }


def public_local_app_icon(
    client: httpx.Client, *, backend_url: str, headers: dict[str, str], marker: str
) -> dict[str, Any]:
    payload = require_object(
        client.post(
            f'{backend_url}/v1/app/generate-icon',
            headers=headers,
            json={
                'name': f'Acceptance {marker}',
                'description': 'A deterministic local deployment acceptance icon',
                'category': 'productivity',
            },
        ),
        'public local app-icon generation',
    )
    try:
        png = base64.b64decode(str(payload.get('icon_base64') or ''), validate=True)
    except ValueError as error:
        raise RuntimeError('public local app-icon response was not valid base64') from error
    if payload.get('mime_type') != 'image/png' or not png.startswith(b'\x89PNG\r\n\x1a\n') or len(png) < 24:
        raise RuntimeError('public local app-icon response was not a real PNG')
    width, height = struct.unpack('>II', png[16:24])
    if (width, height) != (1024, 1024):
        raise RuntimeError('public local app-icon PNG was not 1024x1024')
    return {
        'status': 'passed',
        'transport': 'local_template',
        'public_route_exercised': True,
        'png_decode_exercised': True,
        'dimensions': [width, height],
        'bytes': len(png),
    }


def public_local_file_chat(
    client: httpx.Client,
    *,
    backend_url: str,
    headers: dict[str, str],
    marker: str,
    uid: str,
    db_client: Any,
    storage_client: Any,
) -> dict[str, Any]:
    file_name = 'cutover-acceptance.txt'
    file_content = f'The operator-owned attachment codeword is {marker}.'.encode()
    response = client.post(
        f'{backend_url}/v2/files',
        headers=headers,
        files={'files': (file_name, file_content, 'text/plain')},
        timeout=60,
    )
    if response.status_code >= 400:
        raise RuntimeError(f'public local file-chat upload returned HTTP {response.status_code}')
    payload = response.json()
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError('public local file-chat upload did not return one file record')
    file_record = payload[0]
    file_id = str(file_record.get('id') or '')
    storage_id = str(file_record.get('openai_file_id') or '')
    if not file_id or not storage_id.startswith('local_'):
        raise RuntimeError('public local file-chat upload did not select local_extraction')
    bucket_name = require_environment('BUCKET_CHAT_FILES')
    object_name = f'{uid}/attachments/{storage_id}/{file_name}'
    blob = storage_client.bucket(bucket_name).blob(object_name)
    if not blob.exists():
        raise RuntimeError('public local file-chat upload did not persist the private original')
    answer, _ = public_agent_turn(
        client,
        backend_url=backend_url,
        headers=headers,
        file_ids=[file_id],
        prompt='Read the attached file and return only its exact operator-owned attachment codeword.',
    )
    if marker not in answer:
        raise RuntimeError('public local file-chat answer did not use the uploaded attachment')
    cleanup = client.delete(f'{backend_url}/v1/messages', headers=headers, timeout=60)
    if cleanup.status_code >= 400:
        raise RuntimeError(f'public local file-chat cleanup returned HTTP {cleanup.status_code}')
    if db_client.document(f'users/{uid}/files/{file_id}').get().exists or blob.exists():
        raise RuntimeError('public local file-chat cleanup left a private row or object')
    return {
        'status': 'passed',
        'transport': 'local_extraction',
        'public_upload_route_exercised': True,
        'generic_answer_used_attachment': True,
        'private_object_persisted_before_answer': True,
        'private_object_cleanup_confirmed': True,
        'private_row_cleanup_confirmed': True,
    }


def public_firmware_manifest(
    client: httpx.Client,
    *,
    backend_url: str,
    objects_url: str,
    storage_client: Any,
) -> dict[str, Any]:
    manifest_url = require_environment('FIRMWARE_RELEASE_MANIFEST_URL')
    parsed_manifest = urlsplit(manifest_url)
    parsed_objects = urlsplit(objects_url)
    if (parsed_manifest.scheme, parsed_manifest.netloc) != (parsed_objects.scheme, parsed_objects.netloc):
        raise RuntimeError('firmware manifest is not on PUBLIC_OBJECTS_URL')
    path_parts = unquote(parsed_manifest.path).strip('/').split('/', 1)
    if len(path_parts) != 2 or not all(path_parts):
        raise RuntimeError('firmware manifest URL must identify a MinIO bucket and object')
    bucket_name, manifest_name = path_parts
    bucket = storage_client.bucket(bucket_name)
    manifest_blob = bucket.blob(manifest_name)
    asset_name = f'{Path(manifest_name).parent.as_posix()}/cutover-omi-cv1-9.9.9.zip'.lstrip('./')
    asset_blob = bucket.blob(asset_name)
    asset_payload = b'PK\x03\x04omi-cutover-firmware-fixture'
    release = {
        'tag_name': 'Omi_CV1_v9.9.9',
        'body': (
            '<!-- KEY_VALUE_START\n'
            'release_firmware_version: 9.9.9\n'
            'minimum_firmware_required: 0.0.1\n'
            'is_legacy_secure_dfu: true\n'
            'KEY_VALUE_END -->'
        ),
        'published_at': '2026-08-21T00:00:00Z',
        'draft': False,
        'prerelease': False,
        'assets': [
            {
                'name': 'Omi_CV1_OTA_v9.9.9.zip',
                'browser_download_url': asset_blob.public_url,
            }
        ],
    }
    try:
        asset_blob.upload_from_string(asset_payload, content_type='application/zip')
        asset_blob.make_public()
        manifest_blob.upload_from_string(json.dumps([release], separators=(',', ':')), content_type='application/json')
        manifest_blob.make_public()
        firmware = require_object(
            client.get(
                f'{backend_url}/v2/firmware/stable',
                params={'device_model': 'Omi CV 1'},
                timeout=60,
            ),
            'public operator firmware manifest route',
        )
        if firmware.get('version') != '9.9.9' or firmware.get('zip_url') != asset_blob.public_url:
            raise RuntimeError('public firmware route did not return the operator manifest release')
        asset_response = client.get(str(firmware['zip_url']), timeout=60)
        if asset_response.status_code != 200 or asset_response.content != asset_payload:
            raise RuntimeError('public firmware asset did not roundtrip through PUBLIC_OBJECTS_URL')
    finally:
        for blob in (manifest_blob, asset_blob):
            try:
                if blob.exists():
                    blob.delete()
            except Exception:
                pass
    return {
        'status': 'passed',
        'transport': 'manifest',
        'public_backend_route_exercised': True,
        'manifest_origin_exact': True,
        'asset_origin_exact': True,
        'asset_payload_match': True,
        'github_transport_used': False,
        'fixture_cleanup_confirmed': not manifest_blob.exists() and not asset_blob.exists(),
    }


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
    objects_url = require_environment('PUBLIC_OBJECTS_URL').rstrip('/')
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
    diarization_wav_path = Path(require_environment('SELF_HOST_DIARIZATION_WAV'))
    manifest_path = Path(require_environment('SELF_HOST_CAPTURE_MANIFEST'))
    if os.getenv('SPEAKER_EMBEDDING_PROVIDER') != 'sherpa_onnx':
        raise RuntimeError('assembled live loop requires the local sherpa_onnx speaker-embedding provider')
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
    from database import redis_db
    from database import vector_db
    from database._client import db
    from langchain_core.output_parsers import PydanticOutputParser
    from utils import identity
    from utils.llm.clients import get_llm
    from utils.memory.canonical_consolidation import ConsolidationAgentBatch
    from utils.memory.atom_keyword_index import keyword_search_memory_ids, purge_user_atom_keyword_index
    from utils.memory.canonical_required_processing import invoke_required_memory_processor
    from utils.memory.short_term_promotion import run_canonical_short_term_maintenance
    from utils.other import storage
    from utils.stt import speaker_embedding

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
        signed_object_crud = public_signed_object_crud(
            client,
            objects_url=objects_url,
            marker=marker,
            storage_client=storage.get_storage_client(),
        )
        speaker_vector = speaker_embedding.extract_embedding(str(wav_path))
        speaker_dimension = int(speaker_vector.shape[-1]) if speaker_vector.ndim == 2 else 0
        speaker_norm = float((speaker_vector.astype('float64') ** 2).sum() ** 0.5)
        if speaker_vector.shape[0] != 1 or speaker_dimension <= 0 or not math.isfinite(speaker_norm):
            raise RuntimeError('local speaker embedding returned an invalid vector shape or norm')
        if not 0.999 <= speaker_norm <= 1.001:
            raise RuntimeError('local speaker embedding was not L2 normalized')
        model_artifact_identity = mounted_model_artifact_identity()
        speaker_diarization = probe_mlx_moss_diarization(diarization_wav_path)

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
        realtime_relay = asyncio.run(
            public_realtime_relay_roundtrip(
                client=client,
                backend_url=backend_url,
                origin=origin,
                token=token,
                ca_file=ca_file,
                marker=marker,
            )
        )
        relay_lease_key = f'realtime_relay:lease:{uid}'
        relay_deadline = time.monotonic() + 5
        while time.monotonic() < relay_deadline and redis_db.r.get(relay_lease_key) is not None:
            time.sleep(0.1)
        if redis_db.r.get(relay_lease_key) is not None:
            raise RuntimeError('public realtime relay did not release its cross-instance lease')
        realtime_relay['lease_released'] = True
        local_tts = public_local_tts(client, backend_url=backend_url, headers=auth_headers, marker=marker)
        local_app_icon = public_local_app_icon(client, backend_url=backend_url, headers=auth_headers, marker=marker)
        firmware_manifest = public_firmware_manifest(
            client,
            backend_url=backend_url,
            objects_url=objects_url,
            storage_client=storage.get_storage_client(),
        )
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
        local_file_chat = public_local_file_chat(
            client,
            backend_url=backend_url,
            headers=auth_headers,
            marker=marker,
            uid=uid,
            db_client=db,
            storage_client=storage.get_storage_client(),
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
        keyword_ids = keyword_search_memory_ids(uid, marker, limit=10, db_client=db)
        if memory_id not in keyword_ids:
            raise RuntimeError('Typesense keyword search did not return the promoted canonical memory')

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
        keyword_deleted = purge_user_atom_keyword_index(uid, db_client=db, force=True, raise_on_failure=True)
        if keyword_deleted < 1 or keyword_search_memory_ids(uid, marker, limit=10, db_client=db):
            raise RuntimeError('Typesense keyword deletion did not prove authoritative absence')
        typesense_keyword = {
            'status': 'passed',
            'provider': 'typesense',
            'production_projection_upserted': True,
            'keyword_query_returned_memory_id': True,
            'authoritative_delete_count': keyword_deleted,
            'post_delete_search_empty': True,
        }

        result = {
            'status': 'passed',
            'https_origin_and_hairpin': {
                'public_backend_url': backend_url,
                'public_auth_url': auth_url,
                'public_mcp_url': mcp_url,
                'public_objects_url': objects_url,
                'temporary_ca_verified': True,
                'jwt_issuer_audience_exact': True,
                'public_jwks_kid_present': True,
                'backend_private_jwks_verification': True,
                'auth_private_lifecycle_blocked_at_edge': True,
                'mcp_metadata_exact': True,
                'backend_principal_provisioned_via_public_profile': True,
                'wss_public_origin_exercised': True,
                'public_object_signed_crud': signed_object_crud,
                'realtime_relay_public_wss_exercised': True,
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
                    'speaker_identity_capability': 'sherpa_onnx',
                    'speaker_embedding': {
                        'status': 'passed',
                        'mounted_model_readable': True,
                        'real_wav_decode_exercised': True,
                        'dimension': speaker_dimension,
                        'finite_nonzero_l2_normalized': True,
                    },
                    'speaker_diarization': speaker_diarization,
                    'mounted_model_artifact_identity': model_artifact_identity,
                    'speaker_identity_product_match_exercised': False,
                    'speaker_identity_functional_equivalence_claimed': False,
                },
                'understand': {'generic_model_response_marker': True},
                'realtime_relay': realtime_relay,
                'tts': local_tts,
                'app_icon': local_app_icon,
                'file_chat': local_file_chat,
                'firmware': firmware_manifest,
                'typesense_keyword': typesense_keyword,
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
