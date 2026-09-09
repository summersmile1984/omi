"""Exercise the HTTP/SQL boundary with controlled AI and Vectorize providers."""

import asyncio
import ast
from datetime import datetime, timezone
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import developer_ask_prompt
import developer_ask_routes
from developer_ask_routes import router
from test_developer_routes import AUTHORIZATION, environment
from vector_search import vector_namespace


class Index:
    def __init__(self, ids=(), callback=None):
        self.ids = ids
        self.callback = callback
        self.queries = []

    async def query(self, vector, options):
        self.queries.append(options)
        if self.callback:
            self.callback()
        return {'matches': [{'id': identifier, 'score': 0.9} for identifier in self.ids]}


class AI:
    def __init__(self, callback=None, result=None):
        self.callback = callback
        self.calls = []
        self.result = result or {
            'response': 'You chose September 30[1].',
            'usage': {'prompt_tokens': 123, 'completion_tokens': 10},
        }

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if 'text' in payload:
            return {'data': [[0.01] * 1024]}
        if self.callback:
            self.callback()
        return self.result


def seed(db, identifier, *, uid='developer-user', locked=0, discarded=0, kind='conversation'):
    db.connection.execute(
        'INSERT INTO cf_conversations (uid, id, created_at, updated_at, status, is_locked, discarded, '
        'structured_json, transcript_segments_json) VALUES (?, ?, 1788652800, 1788652800, \'completed\', ?, ?, ?, ?)',
        (
            uid,
            identifier,
            locked,
            discarded,
            json.dumps({'title': identifier, 'overview': 'Release planning'}),
            json.dumps([{'text': f'{identifier}: We chose September 30 for release.'}]),
        ),
    )
    vector = hashlib.sha256(f'{uid}:{identifier}:{kind}'.encode()).hexdigest()
    db.connection.execute(
        'INSERT INTO cf_vector_projection_state (uid, projection_kind, source_id, sub_id, vector_id, source_version, model, updated_at) '
        'VALUES (?, ?, ?, \'\', ?, 1, \'test\', 1)',
        (uid, kind, identifier, vector),
    )
    db.connection.commit()
    return vector


def fixture(**kwargs):
    db, env = environment(**kwargs)
    env.AI = AI()
    env.CONVERSATION_VECTORS = Index([seed(db, 'summary')])
    env.TRANSCRIPT_CHUNK_VECTORS = Index([seed(db, 'transcript', kind='transcript_chunk')])
    return db, env


def call(env, payload=None, authorization=AUTHORIZATION):
    app = FastAPI()
    app.include_router(router)

    @app.middleware('http')
    async def bindings(request, next_handler):
        request.scope['env'] = env
        return await next_handler(request)

    async def invoke():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://core.test') as client:
            return await client.post(
                '/v1/dev/user/ask',
                json=payload or {'question': 'When is the release?'},
                headers={'authorization': authorization} if authorization else {},
            )

    return asyncio.run(invoke())


def generations(env):
    return [payload for _, payload in env.AI.calls if 'messages' in payload]


def test_http_grounded_answer_merges_transcript_first_uses_original_prompt_and_records_usage():
    db, env = fixture()
    response = call(env, {'question': ' When is the release? ', 'timezone': 'Asia/Shanghai', 'limit': 2})
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == {
        'answer': 'You chose September 30[1].',
        'sources': [
            {'id': 'transcript', 'title': 'transcript', 'created_at': '2026-09-06T00:00:00Z'},
            {'id': 'summary', 'title': 'summary', 'created_at': '2026-09-06T00:00:00Z'},
        ],
    }
    prompt = generations(env)[0]['messages'][0]['content']
    assert prompt.index('Conversation "transcript"') < prompt.index('Conversation "summary"')
    assert 'Question\'s timezone: Asia/Shanghai' in prompt
    assert 'You MUST cite the most relevant <memories>' in prompt
    for index in (env.CONVERSATION_VECTORS, env.TRANSCRIPT_CHUNK_VECTORS):
        assert index.queries[0]['namespace'] == vector_namespace('developer-user')
        assert index.queries[0]['returnMetadata'] == 'none'
    assert tuple(
        db.connection.execute(
            'SELECT feature, input_tokens, output_tokens, call_count FROM cf_llm_usage_daily'
        ).fetchone()
    ) == ('chat', 123, 10, 1)
    assert db.connection.execute('SELECT COUNT(*) FROM cf_chat_messages').fetchone()[0] == 0


@pytest.mark.parametrize(
    'locked,discarded,uid', [(1, 0, 'developer-user'), (0, 1, 'developer-user'), (0, 0, 'another-user')]
)
def test_inaccessible_stale_candidates_never_reach_generation(locked, discarded, uid):
    db, env = environment()
    env.AI = AI()
    env.CONVERSATION_VECTORS = Index([seed(db, 'private', uid=uid, locked=locked, discarded=discarded)])
    env.TRANSCRIPT_CHUNK_VECTORS = Index()
    response = call(env)
    assert response.status_code == 200
    assert response.json()['sources'] == []
    assert "couldn't find" in response.json()['answer']
    assert generations(env) == []


def test_transcript_only_match_and_summary_outage_are_distinct_from_empty_retrieval():
    _, env = fixture()
    env.CONVERSATION_VECTORS = Index()
    assert call(env).json()['sources'][0]['id'] == 'transcript'

    def failed():
        raise RuntimeError('private provider details')

    env.CONVERSATION_VECTORS = Index(callback=failed)
    before = len(generations(env))
    response = call(env)
    assert response.status_code == 503
    assert response.json() == {'detail': 'Search temporarily unavailable'}
    assert len(generations(env)) == before


def test_transcript_outage_preserves_summary_with_shared_fallback_telemetry(capsys):
    _, env = fixture()

    def failed():
        raise RuntimeError('private text')

    env.TRANSCRIPT_CHUNK_VECTORS = Index(callback=failed)
    assert call(env).json()['sources'][0]['id'] == 'summary'
    output = capsys.readouterr().out
    assert 'summary_vectorize' in output
    assert 'private text' not in output


@pytest.mark.parametrize(
    'mutation',
    [
        "UPDATE cf_conversations SET is_locked = 1",
        "UPDATE cf_conversations SET structured_json = '{\"title\":\"changed\"}'",
        'DELETE FROM cf_conversations',
        'DELETE FROM cf_developer_api_keys',
        "UPDATE cf_developer_api_keys SET scopes_json = '[]'",
    ],
)
def test_revocation_or_content_changes_during_generation_withhold_answer(mutation):
    db, env = fixture()

    def change():
        db.connection.execute(mutation)
        db.connection.commit()

    env.AI = AI(callback=change)
    response = call(env)
    assert response.status_code == 409
    assert 'September' not in response.text


def test_scope_key_and_input_validation_happen_before_inference():
    _, env = fixture(scopes=['memories:read'])
    assert call(env).status_code == 403
    assert call(env, authorization=None).status_code == 401
    assert call(env, authorization='Bearer omi_mcp_' + '0' * 32).status_code == 403
    assert env.AI.calls == []
    _, env = fixture()
    for payload, status in [
        ({'question': ' '}, 400),
        ({'question': 'x', 'timezone': 'Not/Real'}, 422),
        ({'question': 'x', 'limit': 11}, 422),
        ({'question': 'x' * 1001}, 422),
    ]:
        assert call(env, payload).status_code == status
    assert env.AI.calls == []


def test_permission_change_during_enrichment_prevents_model_disclosure(monkeypatch):
    db, env = fixture()

    async def profile(request, uid):
        db.connection.execute('UPDATE cf_conversations SET is_locked = 1')
        db.connection.commit()
        return {'name': 'Synthetic user'}

    monkeypatch.setattr(developer_ask_routes, '_contact_profile', profile)
    assert call(env).status_code == 409
    assert generations(env) == []


def test_default_visible_facts_are_current_and_rechecked_after_generation():
    db, env = fixture()
    for identifier, content, extra in [
        ('fact', 'User prefers concise answers', ''),
        ('archive', 'ARCHIVE PRIVATE', ", memory_tier = 'archive'"),
        ('locked', 'LOCKED PRIVATE', ', is_locked = 1'),
        ('pending', 'PENDING PRIVATE', ", processing_state = 'pending'"),
        ('sensitive', 'SENSITIVE PRIVATE', ', sensitivity_labels_json = \'["secret"]\''),
    ]:
        db.connection.execute(
            'INSERT INTO cf_memories (uid,id,content,memory_tier,valid_at,created_at,updated_at) VALUES (\'developer-user\',?,?,\'short_term\',unixepoch(),unixepoch(),unixepoch())',
            (identifier, content),
        )
        if extra:
            db.connection.execute('UPDATE cf_memories SET updated_at = 2' + extra + ' WHERE id = ?', (identifier,))
    db.connection.commit()
    result = call(env)
    assert result.status_code == 200, result.text
    prompt = generations(env)[0]['messages'][0]['content']
    assert 'User prefers concise answers' in prompt
    assert 'PRIVATE' not in prompt

    def invalidate():
        db.connection.execute("UPDATE cf_memories SET invalid_at = 2 WHERE id = 'fact'")
        db.connection.commit()

    env.AI = AI(callback=invalidate)
    assert call(env).status_code == 409


def test_original_prompt_bytes_and_request_local_data_adapter(monkeypatch):
    root = Path(__file__).parents[5]
    source = (root / 'backend/utils/llm/chat.py').read_text()
    node = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == '_get_qa_rag_prompt'
    )
    original = '\n'.join(source.splitlines()[node.lineno - 1 : node.end_lineno])

    class FixedDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 6, tzinfo=timezone.utc)

    namespace = {
        'datetime': FixedDate,
        'timezone': timezone,
        'Message': SimpleNamespace(get_messages_as_xml=lambda messages: ''),
        'get_prompt_memories': lambda uid: (None, 'header\nUser fact'),
    }
    exec(compile('from __future__ import annotations\n' + original, '<upstream QA prompt>', 'exec'), namespace)
    monkeypatch.setattr(developer_ask_prompt, 'datetime', FixedDate)
    expected = namespace['_get_qa_rag_prompt']('user', 'question', 'conversation', cited=True, tz='Asia/Shanghai')
    assert (
        developer_ask_prompt.render_prompt('user', 'question', 'conversation', 'header\nUser fact', 'Asia/Shanghai')
        == expected
    )
    assert 'User fact' not in developer_ask_prompt.render_prompt(
        'other', 'question', 'conversation', 'header\nOther fact', 'UTC'
    )


def test_timezone_validation_without_an_os_timezone_database():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, TZPATH, reset_tzpath
    from worker_timezone import load_zoneinfo
    from developer_ask_contract import DeveloperAskRequest

    try:
        reset_tzpath(())
        ZoneInfo.clear_cache()
        assert DeveloperAskRequest(question='when?', timezone='Asia/Shanghai').timezone == 'Asia/Shanghai'
        shanghai = load_zoneinfo('Asia/Shanghai')
        assert datetime(2026, 9, 6, tzinfo=shanghai).utcoffset().total_seconds() == 8 * 3600
        ny = load_zoneinfo('America/New_York')
        assert datetime(2026, 7, 1, tzinfo=ny).utcoffset().total_seconds() == -4 * 3600
        assert datetime(2026, 1, 1, tzinfo=ny).utcoffset().total_seconds() == -5 * 3600
        for key in ('asia/shanghai', '../UTC', '/etc/passwd', 'Not/Real'):
            with pytest.raises(ZoneInfoNotFoundError):
                load_zoneinfo(key)
    finally:
        reset_tzpath(TZPATH)
        ZoneInfo.clear_cache()


def test_malformed_provider_answer_or_usage_never_acknowledges_success():
    _, env = fixture()
    env.AI = AI(result={'response': 'private generated text', 'usage': {}})
    response = call(env)
    assert response.status_code == 503
    assert 'private' not in response.text


@pytest.mark.parametrize('answer', ['The release is October 17.', 'The release is October 17[3].'])
def test_uncited_or_unresolvable_answer_is_withheld_without_retrying_or_inventing_citations(answer):
    db, env = fixture()
    env.AI = AI(result={'response': answer, 'usage': {'prompt_tokens': 123, 'completion_tokens': 10}})
    response = call(env)
    assert response.status_code == 503
    assert response.json() == {'detail': 'Answer temporarily unavailable'}
    assert len(generations(env)) == 1
    assert db.connection.execute('SELECT call_count FROM cf_llm_usage_daily').fetchone()[0] == 1
