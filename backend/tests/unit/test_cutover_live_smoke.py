from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / 'deploy' / 'self-host' / 'cutover-live-smoke.py'
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
