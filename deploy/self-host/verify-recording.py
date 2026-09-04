#!/usr/bin/env python3
"""Exercise public recording/finalization/memory routes with real supplied audio.

This command never seeds business records. A synthetic account's valid JWT and
16kHz mono PCM WAV are inputs; recognition, queue processing and memory writes
must all run through the selected deployment's production handlers.
"""

import argparse
import asyncio
import base64
import json
from pathlib import Path
import time
from urllib.parse import urlencode
import uuid
import wave

import httpx
from websockets.legacy.client import connect


def read_audio(path):
    with wave.open(str(path), 'rb') as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError('supply 16kHz mono signed-16-bit PCM WAV')
        if not 1 <= source.getnframes() <= 16000 * 120:
            raise ValueError('recording must be nonempty and at most two minutes')
        pcm = source.readframes(source.getnframes())
    # A final silent window lets the real streaming recognizer flush its last
    # spoken window before disconnect. This adds audio, never transcript text.
    return pcm + b'\0' * (16000 * 2 * 5)


def require(response, status=200):
    if response.status_code != status:
        raise RuntimeError(f'{response.request.method} {response.request.url.path}: HTTP {response.status_code}')
    return response.json()


async def record(api, token, pcm, expected):
    conversation = None
    transcript = []
    audio_sent = asyncio.Event()
    path = '/v4/listen?' + urlencode(
        {'codec': 'pcm16', 'sample_rate': 16000, 'language': 'en', 'client_conversation_id': str(uuid.uuid4())}
    )
    url = api.replace('http://', 'ws://').replace('https://', 'wss://') + path
    async with connect(
        url, extra_headers={'Authorization': 'Bearer ' + token}, open_timeout=30, max_size=1024 * 1024, ping_interval=10
    ) as socket:

        async def sender():
            for start in range(0, len(pcm), 8000):
                await socket.send(pcm[start : start + 8000])
                await asyncio.sleep(0.25)
            audio_sent.set()

        send = asyncio.create_task(sender())
        try:
            async with asyncio.timeout(len(pcm) / 32000 + 90):
                while True:
                    raw = await socket.recv()
                    event = {} if raw == 'ping' else json.loads(raw)
                    if isinstance(event, dict) and event.get('type') == 'conversation_session':
                        conversation = event['conversation_id']
                        print(json.dumps({'phase': 'recording_session', 'conversation_id': conversation}), flush=True)
                    if isinstance(event, list):
                        transcript.extend(str(segment.get('text', '')) for segment in event)
                        print(
                            json.dumps(
                                {
                                    'phase': 'recognized',
                                    'segments': len(event),
                                    'expected_phrase': expected.casefold() in ' '.join(transcript).casefold(),
                                }
                            ),
                            flush=True,
                        )
                    if audio_sent.is_set() and conversation and expected.casefold() in ' '.join(transcript).casefold():
                        break
        finally:
            send.cancel()
            await asyncio.gather(send, return_exceptions=True)
    return conversation


async def verify(args):
    secret = json.loads(args.token_file.read_text())
    token = secret['token']
    pcm = read_audio(args.audio_file)
    api = args.api.rstrip('/')
    async with httpx.AsyncClient(base_url=api, headers={'Authorization': 'Bearer ' + token}, timeout=630) as client:
        # Public preference mutation creates/updates the existing user document.
        require(await client.patch('/v1/users/language', json={'language': 'en'}))
        conversation_id = await record(api, token, pcm, args.transcript_contains)
        path = '/v1/conversations/' + conversation_id
        detail = require(await client.get(path))
        segments = detail.get('transcript_segments', [])
        if (
            args.transcript_contains.casefold()
            not in ' '.join(segment.get('text', '') for segment in segments).casefold()
        ):
            raise AssertionError('recognized transcript was not durably persisted')
        # Explicit user assignment is part of the public contract; local STT
        # does not claim automatic speaker identity or enable speech training.
        for speaker in sorted({segment['speaker_id'] for segment in segments}):
            require(
                await client.patch(
                    path + '/assign-speaker/' + str(speaker),
                    params={'assign_type': 'is_user', 'value': 'true', 'use_for_speech_training': 'false'},
                )
            )
        accepted = require(await client.post(path + '/finalize', json={}))
        print(
            json.dumps(
                {
                    'phase': 'recorded',
                    'conversation_id': conversation_id,
                    'segments': len(segments),
                    'finalize_status': accepted['conversation']['status'],
                }
            ),
            flush=True,
        )
        deadline = time.monotonic() + args.timeout
        last = None
        while time.monotonic() < deadline:
            status = require(await client.get(path + '/finalization'))
            if status != last:
                print(json.dumps({'phase': 'finalization', 'status': status}), flush=True)
                last = status
            detail = require(await client.get(path))
            if finalization_complete(status, detail):
                break
            await asyncio.sleep(2)
        else:
            raise TimeoutError('actual queue/finalizer did not complete')
        memories = require(await client.get('/v3/memories', params={'limit': 100, 'include_archive': 'true'}))
        matching = [memory for memory in memories if args.memory_contains.casefold() in json.dumps(memory).casefold()]
        if not matching:
            raise AssertionError('completed conversation produced no expected canonical memory')
        print(
            json.dumps(
                {
                    'phase': 'completed',
                    'conversation_id': conversation_id,
                    'memories': matching,
                    'action_items': require(await client.get('/v1/action-items')),
                }
            ),
            flush=True,
        )
        # Retrieve through the normal persisted chat/tool loop. A guessed answer
        # without a memory/conversation tool progress event is not sufficient.
        stream = await client.post('/v2/messages', json={'text': args.question})
        if stream.status_code != 200:
            raise AssertionError('retrieval chat did not start')
        answer = decode_answer(stream.text, args.memory_contains)
        history = require(await client.get('/v2/messages'))
        if not any(message.get('id') == answer['id'] and message.get('text') == answer['text'] for message in history):
            raise AssertionError('retrieval answer was not durably persisted in chat history')
        print(json.dumps({'phase': 'retrieved', 'message_id': answer['id'], 'text': answer['text']}), flush=True)


def finalization_complete(status, detail):
    # Summary completion can precede canonical-memory fanout. The durable job
    # owns finalization completion; observing its earlier conversation write is
    # insufficient evidence that derived data has committed.
    if status.get('terminal') is not True:
        return False
    if status.get('status') != 'completed':
        raise AssertionError('finalization reached a non-success terminal state')
    return detail.get('status') == 'completed'


def decode_answer(wire, expected):
    frames = [part.strip() for part in wire.split('\n\n') if part.strip()]
    terminal = [part[6:] for part in frames if part.startswith('done: ')]
    if any(part.startswith('error:') for part in frames) or len(terminal) != 1:
        raise AssertionError('retrieval chat did not produce exactly one successful terminal frame')
    answer = json.loads(base64.b64decode(terminal[0], validate=True))
    if (
        not isinstance(answer, dict)
        or not answer.get('id')
        or expected.casefold() not in answer.get('text', '').casefold()
    ):
        raise AssertionError('retrieval answer does not contain the expected fact')
    if not any(part.startswith(('think: Searching memories', 'think: Searching conversations')) for part in frames):
        raise AssertionError('retrieval chat did not run a personal-history tool')
    return answer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', required=True)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--audio-file', required=True, type=Path)
    parser.add_argument('--transcript-contains', required=True)
    parser.add_argument('--memory-contains', required=True)
    parser.add_argument('--question', required=True)
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1800:
        parser.error('timeout must be between 1 and 1800 seconds')
    asyncio.run(verify(args))


if __name__ == '__main__':
    main()
