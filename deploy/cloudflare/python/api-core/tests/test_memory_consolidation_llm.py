"""Exercise original prompts, provider responses and D1 application together."""

import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

import memory_consolidation_llm as llm
import memory_kernel_consolidation as policy
from memory_apply_item import read_item
from test_memory_consolidation_apply import context, decision, environment
from test_memory_mutation_lock import target
from test_memory_review_routes import journal


class Provider:
    def __init__(self, output=None, *, error=None, mutate=None, usage=None, wire_error=None):
        self.output = output
        self.wire_error = wire_error
        self.error = error
        self.mutate = mutate
        self.usage = usage if usage is not None else {'prompt_tokens': 1234, 'completion_tokens': 234}
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if self.mutate:
            self.mutate()
        if self.error:
            raise self.error
        # The selected model's documented Chat Completions wire format.
        content = json.dumps(self.output) if isinstance(self.output, dict) else self.output
        message = {'role': 'assistant', 'content': content, 'refusal': None}
        choice = {'index': 0, 'finish_reason': 'stop', 'message': message}
        choices = [choice]
        if self.wire_error == 'length':
            choice['finish_reason'] = 'length'
        elif self.wire_error == 'refusal':
            message['refusal'] = 'Model declined'
        elif self.wire_error == 'reasoning-only':
            message.update(content=None, reasoning_content=content)
        elif self.wire_error == 'extra-choice':
            choices.append(choice.copy())
        return {'choices': choices, 'usage': self.usage}


def execute(database, snapshot, provider):
    env = environment(database)
    env.AI = provider
    return asyncio.run(llm.consolidate_with_llm(env, snapshot, run_id='model-test'))


@pytest.mark.parametrize('older_runtime_schema', [False, True])
def test_original_default_prompt_schema_and_sensitive_context_are_preserved(target, monkeypatch, older_runtime_schema):
    monkeypatch.delenv('MEMORY_BELIEF_MODEL_ENABLED', raising=False)
    assert llm.CONSOLIDATION_OUTPUT_SCHEMA == policy.ConsolidationAgentBatch.model_json_schema()
    if older_runtime_schema:
        # Actual hosted Pydantic 2.10 omits this unconstrained-dict property;
        # backend 2.11 emits it, changing the otherwise unchanged prompt text.
        schema = policy.ConsolidationAgentBatch.model_json_schema()
        arguments = schema['$defs']['ConsolidationAgentDecision']['properties']['arguments']
        assert arguments.pop('additionalProperties') is True
        monkeypatch.setattr(policy.ConsolidationAgentBatch, 'model_json_schema', lambda: schema)
    database, _, create = target
    source = create(
        content='Ordinary input with 中文 and "quotes"', subject_entity_id='user', subject_attribution='user'
    )
    restricted = create(content='Sensitive marker must stay private')
    database.connection.execute(
        "UPDATE cf_memories SET sensitivity_labels_json='[\"secret\"]' WHERE id=?", (restricted,)
    )
    snapshot = context(database, source, restricted, candidates=[restricted])
    messages = llm.model_messages(snapshot)
    assert [message['role'] for message in messages] == ['system', 'user']
    # Captured with the original constructor and the backend's pinned real
    # langchain-core 1.3.3 PydanticOutputParser, not this Worker formatter.
    golden = (Path(__file__).parent / 'fixtures/consolidation-original-prefix.txt').read_text()
    assert messages[0]['content'] == golden
    assert messages[1]['content'] == 'Batch JSON:\n' + policy.format_consolidation_llm_context(snapshot)
    assert 'Sensitive marker' not in json.dumps(messages)
    assert 'REDACTED: restricted sensitivity' in messages[1]['content']
    assert 'Ordinary input' in messages[1]['content']
    assert read_item(database.row(source)).processing_state.value == 'pending'


@pytest.mark.parametrize('wire_format', ['json', 'fenced-json'])
def test_one_model_response_drives_all_four_real_apply_routes_and_usage(target, wire_format):
    database, request, create = target
    ids = [
        create(content='Prefers jasmine tea', subject_entity_id='user', subject_attribution='user') for _ in range(4)
    ]
    snapshot = context(database, *ids)
    batch = policy.ConsolidationAgentBatch(
        decisions=[
            decision(item, route)
            for item, route in zip(snapshot.pending_items, ['promote', 'archive', 'review', 'reject'])
        ]
    )
    output = batch.model_dump(mode='json') if wire_format == 'json' else '```json\n' + batch.model_dump_json() + '\n```'
    provider = Provider(output)
    result = execute(database, snapshot, provider)
    assert len(provider.calls) == 1
    model, payload = provider.calls[0]
    assert model == '@cf/qwen/qwen3.8-27b'
    # Original _invoke_consolidation_llm calls invoke(messages); its parser
    # validates afterwards. The hosted Qwen regression exposed decoder drift
    # when the adapter added response_format despite that contract.
    assert 'response_format' not in payload
    assert len(payload['messages']) == 2 and payload['temperature'] == 0
    assert payload['max_completion_tokens'] == 8192 and payload['n'] == 1
    assert payload['reasoning_effort'] == 'medium'
    for key, route in zip(ids, ['promote', 'archive', 'review', 'reject']):
        item = read_item(database.row(key))
        assert item.promotion['route'] == route
        assert item.item_revision == 3
        assert item.promotion['processing_receipt']['input_item_revision'] == 1
        assert item.promotion['processing_receipt']['output_item_revision'] == 2
        assert result[key] == item
    assert [item['id'] for item in request('GET', '/v3/memories').json()] == [ids[0]]
    usage = dict(database.connection.execute("SELECT * FROM cf_llm_usage_daily WHERE uid='owner'").fetchone())
    assert (usage['feature'], usage['model'], usage['input_tokens'], usage['output_tokens'], usage['call_count']) == (
        'memory_consolidation',
        model,
        1234,
        234,
        1,
    )


@pytest.mark.parametrize(
    'failure',
    [
        'provider',
        'timeout',
        'json',
        'usage',
        'omitted',
        'incomplete-promote',
        'foreign-evidence',
        'foreign-target',
        'length',
        'refusal',
        'reasoning-only',
        'extra-choice',
    ],
)
def test_failed_or_invalid_inference_never_fabricates_a_route(target, failure):
    database, _, create = target
    key = create(subject_entity_id='user', subject_attribution='user')
    snapshot = context(database, key)
    output = policy.ConsolidationAgentBatch(decisions=[decision(snapshot.pending_items[0])]).model_dump(mode='json')
    kwargs = {}
    if failure == 'provider':
        kwargs['error'] = RuntimeError('private provider payload')
    elif failure == 'timeout':
        kwargs['error'] = TimeoutError('provider timeout')
    elif failure == 'json':
        output = 'incomplete { response'
    elif failure == 'usage':
        kwargs['usage'] = {'prompt_tokens': True, 'completion_tokens': -1}
    elif failure == 'omitted':
        output['decisions'] = []
    elif failure == 'incomplete-promote':
        for field in ['memory_text', 'evidence_ids', 'predicate', 'arguments']:
            output['decisions'][0].pop(field, None)
    elif failure in ['length', 'refusal', 'reasoning-only', 'extra-choice']:
        kwargs['wire_error'] = failure
    elif failure == 'foreign-evidence':
        output['decisions'][0]['evidence_ids'] = ['foreign']
    else:
        output['decisions'][0].update(reconciliation='replace', target_memory_id='foreign', supersedes=['foreign'])
    provider = Provider(output, **kwargs)
    before = journal(database)
    with pytest.raises((llm.ConsolidationInferenceError, policy.ConsolidationApplySkipped)):
        execute(database, snapshot, provider)
    assert len(provider.calls) == 1
    assert journal(database) == before
    assert read_item(database.row(key)).processing_state.value == 'pending'


def test_source_change_before_disclosure_skips_model_and_change_during_inference_skips_apply(target):
    database, _, create = target
    key = create(subject_entity_id='user', subject_attribution='user')
    snapshot = context(database, key)
    output = policy.ConsolidationAgentBatch(decisions=[decision(snapshot.pending_items[0])]).model_dump(mode='json')
    database.connection.execute('UPDATE cf_memories SET content=? WHERE id=?', ('Concurrent correction', key))
    provider = Provider(output)
    with pytest.raises(policy.ConsolidationApplySkipped):
        execute(database, snapshot, provider)
    assert provider.calls == []
    snapshot = context(database, key)
    changed = {}

    def mutate():
        database.connection.execute('UPDATE cf_memories SET content=? WHERE id=?', ('Another correction', key))
        changed['journal'] = journal(database)

    provider = Provider(output, mutate=mutate)
    with pytest.raises(policy.ConsolidationApplySkipped):
        execute(database, snapshot, provider)
    assert len(provider.calls) == 1
    assert journal(database) == changed['journal']


def test_oversized_context_and_duplicate_source_do_not_call_provider(target, monkeypatch):
    database, _, create = target
    key = create()
    snapshot = context(database, key)
    provider = Provider({})
    monkeypatch.setattr(llm, 'MAX_INPUT_BYTES', 1)
    with pytest.raises(llm.ConsolidationInferenceError, match='input_too_large'):
        execute(database, snapshot, provider)
    assert provider.calls == []
    snapshot.pending_items.append(snapshot.pending_items[0])
    with pytest.raises(ValueError, match='duplicate'):
        execute(database, snapshot, provider)
    assert provider.calls == []


def test_account_deletion_during_hydration_prevents_model_disclosure(target, monkeypatch):
    database, _, create = target
    snapshot = context(database, create())
    original = llm._hydrate_context

    async def hydrate(*args):
        result = await original(*args)
        database.connection.execute(
            "INSERT INTO cf_account_deletion_intents "
            "(uid,job_id,status,phase,next_attempt_at,created_at,updated_at) "
            "VALUES ('owner','test-deletion','pending','quiescing',1,1,1)"
        )
        return result

    monkeypatch.setattr(llm, '_hydrate_context', hydrate)
    provider = Provider({})
    with pytest.raises(ValueError, match='account_deleted'):
        execute(database, snapshot, provider)
    assert provider.calls == []
