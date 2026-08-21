from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / 'deploy' / 'self-host' / 'cutover-live-smoke.py'
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location('cutover_live_smoke', SCRIPT)
assert SPEC and SPEC.loader
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class _Stream:
    status_code = 200

    def __init__(self, lines: list[str]):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self):
        return iter(self._lines)


class _Client:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.request = None

    def stream(self, method, url, **kwargs):
        self.request = (method, url, kwargs)
        return _Stream(self.lines)


def _done(text: str) -> str:
    encoded = base64.b64encode(json.dumps({'text': text}).encode()).decode()
    return f'done: {encoded}'


def test_public_agent_web_search_requires_product_sse_tool_event_and_wikipedia_source():
    client = _Client(
        [
            'think: Thinking',
            'think: Searching the web',
            _done('cutover-marker source https://en.wikipedia.org/wiki/The_Wizard_of_Oz'),
        ]
    )

    result = SMOKE.public_agent_web_search(
        client,
        backend_url='https://api.omi.test',
        headers={'authorization': 'Bearer token'},
        marker='cutover-marker',
    )

    assert result['web_search_tool_event'] is True
    assert result['source_url'].startswith('https://en.wikipedia.org/')
    assert client.request[0:2] == ('POST', 'https://api.omi.test/v2/messages')


def test_public_agent_web_search_rejects_answer_without_tool_execution_event():
    client = _Client([_done('cutover-marker https://en.wikipedia.org/wiki/The_Wizard_of_Oz')])

    with pytest.raises(RuntimeError, match='did not emit a web-search tool execution event'):
        SMOKE.public_agent_web_search(
            client,
            backend_url='https://api.omi.test',
            headers={'authorization': 'Bearer token'},
            marker='cutover-marker',
        )


def test_public_understand_requires_marker_and_transcript_terms_on_public_route():
    client = _Client([_done('cutover-marker: the complaint was against a wizard.')])

    answer = SMOKE.public_understand(
        client,
        backend_url='https://api.omi.test',
        headers={'authorization': 'Bearer token'},
        transcript='A confused complaint was made against the wizard.',
        expected_transcript='A confused complaint was made against the wizard.',
        marker='cutover-marker',
    )

    assert 'cutover-marker' in answer
    assert client.request[0:2] == ('POST', 'https://api.omi.test/v2/messages')


def test_public_understand_rejects_answer_without_per_run_marker():
    client = _Client([_done('The complaint was made against a wizard.')])

    with pytest.raises(RuntimeError, match='did not preserve the per-run marker'):
        SMOKE.public_understand(
            client,
            backend_url='https://api.omi.test',
            headers={'authorization': 'Bearer token'},
            transcript='A confused complaint was made against the wizard.',
            expected_transcript='A confused complaint was made against the wizard.',
            marker='cutover-marker',
        )


def test_listen_wire_heartbeat_is_not_parsed_as_json():
    assert SMOKE.parse_ws_event('ping') is None
    assert SMOKE.parse_ws_event('{"type":"service_status","status":"ready"}') == {
        'type': 'service_status',
        'status': 'ready',
    }
    with pytest.raises(RuntimeError, match='malformed non-heartbeat'):
        SMOKE.parse_ws_event('not-json')


def test_mlx_moss_catalog_and_segments_require_exact_model_two_speakers_and_multiple_transitions():
    model = 'kuotient/MOSS-Transcribe-Diarize-MLX-8bit'
    catalog = {
        'object': 'list',
        'data': [{'id': model, 'object': 'model', 'created': 1, 'owned_by': 'system'}],
    }
    assert SMOKE._require_mlx_moss_model_catalog(catalog, model) == [model]
    summary = SMOKE._summarize_mlx_moss_segments(
        [
            {'timestamp': [0.0, 1.0], 'speaker': 'SPEAKER_01', 'text': 'first'},
            {'timestamp': [1.0, 2.0], 'speaker': 'SPEAKER_02', 'text': 'second'},
            {'timestamp': [2.0, 3.0], 'speaker': 'SPEAKER_01', 'text': 'third'},
        ],
        audio_duration_seconds=3.0,
    )
    assert summary['speaker_count'] == 2
    assert summary['speaker_transition_count'] == 2
    assert summary['last_segment_end_seconds'] == 3.0

    with pytest.raises(RuntimeError, match='exact configured diarization model id'):
        SMOKE._require_mlx_moss_model_catalog(catalog, 'another-model')
    with pytest.raises(RuntimeError, match='at least two speakers'):
        SMOKE._summarize_mlx_moss_segments(
            [{'timestamp': [0.0, 1.0], 'speaker': 'SPEAKER_01', 'text': 'only'}],
            audio_duration_seconds=1.0,
        )
    with pytest.raises(RuntimeError, match='multiple speaker transitions'):
        SMOKE._summarize_mlx_moss_segments(
            [
                {'timestamp': [0.0, 1.0], 'speaker': 'SPEAKER_01', 'text': 'first'},
                {'timestamp': [1.0, 2.0], 'speaker': 'SPEAKER_02', 'text': 'second'},
            ],
            audio_duration_seconds=2.0,
        )
