#!/usr/bin/env python3
"""Public chat and speech acceptance using a newly owned synthetic account."""

import argparse
import base64
import json
import io
from pathlib import Path
import re
import sys
import wave

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'contracts/deployment'))
from core import ProductContract, require


def verify_chat_stream(text):
    # The shipped Web consumer decodes data: text and base64 done: JSON.
    lines = text.splitlines()
    require(not any(line.startswith('error:') for line in lines), 'chat emitted an error')
    terminal = [line[6:] for line in lines if line.startswith('done: ')]
    require(len(terminal) == 1, 'chat must complete exactly once')
    answer = json.loads(base64.b64decode(terminal[0], validate=True))
    streamed = ''.join(line[6:].replace('__CRLF__', '\n') for line in lines if line.startswith('data: '))
    # utils.chat_followup.split_followup_tail rstrips the visible answer when
    # removing the follow-up chip. Already emitted whitespace remains in SSE.
    require(
        bool(streamed.strip()) and streamed.rstrip() == str(answer.get('text', '')).rstrip(),
        'chat stream differs from completed answer',
    )
    return answer


def verify_transcript(value):
    require(value.get('stt_provider') in {'mimo', 'sensevoice'}, 'ASR did not identify the selected Server provider')
    require(
        isinstance(value.get('transcript'), str) and re.search('笔记|工作', value['transcript']),
        'ASR did not recognize the synthetic work-notes phrase',
    )


def run(metadata):
    metadata = {**metadata, 'trace_dir': str(Path(metadata['trace_dir']) / 'ai')}
    contract = ProductContract(metadata, remote=True)
    try:
        owner = contract.signup()
        with httpx.Client(
            base_url=metadata['api_origin'],
            follow_redirects=False,
            timeout=180,
            headers={'Authorization': 'Bearer ' + owner.jwt, 'X-App-Platform': 'web'},
        ) as client:

            def request(method, path, **kwargs):
                response = client.request(method, path, **kwargs)
                require(response.status_code == 200, f'AI {method} {path} returned HTTP {response.status_code}')
                require(len(response.content) < 4 * 1024 * 1024, 'AI acceptance response exceeded its bound')
                return response

            def chat():
                session = request('POST', '/v2/chat-sessions', json={'title': 'Release AI acceptance'}).json()['id']
                response = request(
                    'POST',
                    '/v2/messages',
                    params={'chat_session_id': session},
                    json={'text': '你好，请给我一句整理工作笔记的建议。'},
                )
                require('text/event-stream' in response.headers.get('content-type', ''), 'chat did not stream')
                answer = verify_chat_stream(response.text)
                history = request('GET', '/v2/messages', params={'chat_session_id': session}).json()
                require(
                    any(row.get('id') == answer.get('id') and row.get('text') == answer['text'] for row in history),
                    'completed AI answer was not persisted',
                )

            contract.case('ai.public-streamed-chat-and-history', chat)

            def speech_audio():
                audio = request(
                    'POST', '/v2/tts/synthesize', json={'text': '今天整理工作笔记。', 'output_format': 'wav'}
                )
                require(
                    audio.headers.get('content-type', '').startswith('audio/wav') and len(audio.content) > 1000,
                    'TTS omitted WAV audio',
                )
                return audio.content

            audio_bytes = None

            def speech_pcm():
                nonlocal audio_bytes
                audio_bytes = speech_audio()
                with wave.open(io.BytesIO(audio_bytes)) as audio:
                    require(audio.getsampwidth() == 2, 'TTS WAV is not PCM16')
                    channels, sample_rate = audio.getnchannels(), audio.getframerate()
                    pcm = audio.readframes(audio.getnframes())
                transcript = request(
                    'POST',
                    '/v2/voice-message/transcribe',
                    content=pcm,
                    headers={'Content-Type': 'application/octet-stream'},
                    params={'language': 'zh', 'sample_rate': sample_rate, 'channels': channels, 'encoding': 'linear16'},
                ).json()
                verify_transcript(transcript)

            contract.case('ai.public-tts-to-pcm-asr', speech_pcm)

            def speech_upload():
                transcript = request(
                    'POST',
                    '/v2/voice-message/transcribe',
                    files={'files': ('release.wav', audio_bytes or speech_audio(), 'audio/wav')},
                    data={'language': 'zh'},
                ).json()
                verify_transcript(transcript)

            contract.case('ai.public-tts-to-upload-asr', speech_upload)
    finally:
        report = contract.report()
        report['scope'] = 'public-chat-history-and-tts-asr-roundtrip'
        (Path(metadata['trace_dir']) / 'ai-results.json').write_text(json.dumps(report, indent=2) + '\n')
    require(report['passed'], 'Server public AI acceptance failed; inspect the private ai-results.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata', required=True, type=Path)
    args = parser.parse_args()
    run(json.loads(args.metadata.read_text()))
