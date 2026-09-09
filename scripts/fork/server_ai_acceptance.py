#!/usr/bin/env python3
"""Public chat and speech acceptance using a newly owned synthetic account."""

import argparse
import base64
import json
from pathlib import Path
import re
import sys

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
    require(bool(streamed.strip()) and streamed == answer.get('text'), 'chat stream differs from completed answer')
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
                require(response.status_code == 200, f'AI acceptance returned HTTP {response.status_code}')
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

            def speech():
                audio = request('POST', '/v1/tts/synthesize', json={'text': '今天整理工作笔记。', 'voice_id': 'alloy'})
                require(
                    audio.headers.get('content-type', '').startswith('audio/mpeg') and len(audio.content) > 1000,
                    'TTS omitted MP3 audio',
                )
                transcript = request(
                    'POST',
                    '/v2/voice-message/transcribe',
                    files={'file': ('release.mp3', audio.content, 'audio/mpeg')},
                    data={'language': 'zh'},
                ).json()
                verify_transcript(transcript)

            contract.case('ai.public-tts-to-asr', speech)
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
