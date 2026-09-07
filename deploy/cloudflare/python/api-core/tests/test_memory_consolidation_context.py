"""Actual native intake, vector hydration, upstream context and inference/apply."""

import asyncio
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from memory_apply_item import read_item
from memory_apply_edit import edit_native_memory
from memory_consolidation_context import gather_consolidation_context, embedding_windows
from memory_consolidation_llm import consolidate_with_llm, model_messages
from memory_kernel_consolidation import ConsolidationApplySkipped, bound_rejected_memory_examples
from vector_search import VECTOR_MODEL, vector_namespace
from test_memory_consolidation_apply import context, decision, environment, execute
from test_memory_mutation_lock import target
from test_memory_review_routes import journal


async def invoke_context(env, uid, ids, *, run_id):
    context = await gather_consolidation_context(env, uid, ids)
    return await consolidate_with_llm(env, context, run_id=run_id)


class Index:
    def __init__(self):
        self.matches = []
        self.calls = []
        self.error = None
        self.visible = True
        self.readiness_calls = []

    async def queryById(self, vector_id, options):
        self.readiness_calls.append((vector_id, options))
        return {'matches': [{'id': vector_id, 'score': 1.0}] if self.visible else []}

    async def query(self, vector, options):
        self.calls.append(options)
        if self.error:
            raise self.error
        matches = self.matches(len(self.calls)) if callable(self.matches) else self.matches
        return {'matches': matches}


class Provider:
    def __init__(self, *, output=None, on_embedding=None):
        self.calls = []
        self.output = output
        self.on_embedding = on_embedding

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if model == VECTOR_MODEL:
            if self.on_embedding:
                await self.on_embedding()
            return {'data': [[0.1] * 1024]}
        assert self.output is not None
        return {
            'choices': [
                {
                    'finish_reason': 'stop',
                    'message': {
                        'role': 'assistant',
                        'content': json.dumps(self.output),
                    },
                }
            ],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20},
        }


def services(database, **kwargs):
    env = environment(database)
    env.AI = Provider(**kwargs)
    env.MEMORY_VECTORS = Index()
    return env


def long_term(database, create, content):
    key = create(content=content, subject_entity_id='user', subject_attribution='user')
    snap = context(database, key)
    execute(database, snap, decision(snap.pending_items[0], memory_text=content))
    return key


def project(database, key, vector_id, *, uid='owner', revision=None, publication_size=1):
    row = database.row(key)
    revision = row['item_revision'] if revision is None else revision
    database.connection.execute(
        'INSERT INTO cf_vector_projection_state '
        '(uid,projection_kind,source_id,sub_id,vector_id,source_version,model,updated_at) '
        "VALUES (?, 'memory', ?, ?, ?, ?, ?, 1)",
        (uid, key, vector_id, vector_id, revision, VECTOR_MODEL),
    )
    database.connection.execute(
        'INSERT INTO cf_memory_vector_artifacts '
        '(vector_id,uid,source_id,attempt_id,sub_id,source_version,model,writer_until,writer_done,publication_size) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?)",
        (vector_id, uid, key, 'fixture:' + vector_id, vector_id, revision, VECTOR_MODEL, publication_size),
    )


def test_retrieval_and_model_route_join_native_source_and_real_sql_apply(target):
    database, request, create = target
    old = long_term(database, create, 'I drink jasmine tea every morning')
    project(database, old, 'a' * 64)
    source = create(content='I drink jasmine tea every morning', subject_entity_id='user', subject_attribution='user')
    pending = read_item(database.row(source))
    outcome = decision(pending, 'archive', reconciliation='duplicate', target_memory_id=old)
    env = services(database, output={'decisions': [outcome.model_dump(mode='json')]})
    env.MEMORY_VECTORS.matches = [{'id': 'a' * 64, 'score': 0.94}]
    result = asyncio.run(invoke_context(env, 'owner', [source], run_id='actual-context'))
    assert result[source].promotion['route'] == 'archive'
    assert [model for model, payload in env.AI.calls] == [VECTOR_MODEL, '@cf/qwen/qwen3.8-27b']
    assert env.MEMORY_VECTORS.calls == [
        {
            'topK': 9,
            'namespace': vector_namespace('owner'),
            'returnValues': False,
            'returnMetadata': 'none',
        }
    ]
    model_input = json.loads(env.AI.calls[1][1]['messages'][1]['content'].split('Batch JSON:\n')[1])
    candidate = model_input['candidate_groups'][0]['candidates'][0]
    assert candidate['memory_id'] == old and candidate['score'] == 0.94
    assert [item['id'] for item in request('GET', '/v3/memories').json()] == [old]


def test_stale_and_foreign_vector_ids_never_supply_prompt_content(target):
    database, _, create = target
    old = long_term(database, create, 'Fresh source')
    stale = long_term(database, create, 'Stale source')
    project(database, old, 'a' * 64)
    project(database, stale, 'b' * 64, revision=0)
    database.connection.execute('DELETE FROM cf_vector_projection_state WHERE vector_id = ?', ('b' * 64,))
    project(database, stale, 'd' * 64)
    project(database, old, 'c' * 64, uid='other')
    source = create(content='Fresh source')
    env = services(database)
    env.MEMORY_VECTORS.matches = [
        {'id': 'c' * 64, 'score': 0.99},
        {'id': 'b' * 64, 'score': 0.95},
        {'id': 'a' * 64, 'score': 0.9},
        {'id': 'a' * 64, 'score': 0.8},
    ]
    result = asyncio.run(gather_consolidation_context(env, 'owner', [source]))
    assert [(item.memory_id, item.score) for item in result.candidates_by_anchor[source]] == [(old, 0.9)]
    assert 'Stale source' not in str(model_messages(result))


def test_hidden_rejected_feedback_keeps_upstream_bounds_and_sensitive_exclusion(target):
    database, request, create = target
    rejected = create(content=('Never  claim\n this about me. ' * 25))
    assert request('POST', '/v3/memories/' + rejected + '/review?value=false').status_code == 200
    database.connection.execute("UPDATE cf_memories SET status='hidden' WHERE id=?", (rejected,))
    secret = create(content='A rejected sensitive example must never reach the model')
    assert request('POST', '/v3/memories/' + secret + '/review?value=false').status_code == 200
    database.connection.execute("UPDATE cf_memories SET sensitivity_labels_json='[\"health\"]' WHERE id=?", (secret,))
    source = create(content='A new observation')
    env = services(database)
    result = asyncio.run(gather_consolidation_context(env, 'owner', [source]))
    assert [item.memory_id for item in result.owner_rejected_examples] == [rejected]
    text = bound_rejected_memory_examples([read_item(database.row(rejected)).content])[0]
    assert result.owner_rejected_examples[0].content == text and len(text) == 180
    assert 'rejected sensitive example' not in str(model_messages(result))
    assert read_item(database.row(rejected)).status.value == 'hidden'


def test_full_long_source_is_searched_including_a_match_at_the_end(target):
    database, _, create = target
    old = long_term(database, create, 'Only the end of the source matches')
    project(database, old, 'a' * 64)
    text = '首段。' + '中间内容。' * 2_000 + 'Only the end of the source matches'
    source = create(content=text)
    windows = list(embedding_windows(text))
    env = services(database)
    env.MEMORY_VECTORS.matches = lambda n: [{'id': 'a' * 64, 'score': 0.91}] if n == len(windows) else []
    result = asyncio.run(gather_consolidation_context(env, 'owner', [source]))
    assert [payload['text'][0] for model, payload in env.AI.calls] == windows
    assert windows[0].startswith('首段。') and windows[-1].endswith('Only the end of the source matches')
    assert max(map(len, windows)) <= 3500
    assert [item.memory_id for item in result.candidates_by_anchor[source]] == [old]


@pytest.mark.parametrize('fault', ['provider', 'source_changed', 'foreign_source'])
def test_context_failure_never_calls_model_or_applies_a_business_route(target, fault):
    database, request, create = target
    source = create(content='Original observation')
    env = services(database)
    uid = 'owner'
    if fault == 'provider':
        env.MEMORY_VECTORS.error = RuntimeError('synthetic dependency failure')
    elif fault == 'source_changed':

        async def mutate():
            assert await edit_native_memory(env, 'owner', source, 'Changed', int(time.time()))

        env.AI.on_embedding = mutate
    else:
        uid = 'another-owner'
    before = journal(database)
    expected = {
        'provider': 'candidates_unavailable',
        'source_changed': 'authority_changed',
        'foreign_source': 'source_changed',
    }[fault]
    with pytest.raises(ConsolidationApplySkipped, match=expected):
        asyncio.run(invoke_context(env, uid, [source], run_id='fault-case'))
    assert all(model == VECTOR_MODEL for model, payload in env.AI.calls)
    assert read_item(database.row(source)).tier.value == 'short_term'
    if fault != 'source_changed':
        assert journal(database) == before
    if fault == 'source_changed':
        assert read_item(database.row(source)).content == 'Changed'
