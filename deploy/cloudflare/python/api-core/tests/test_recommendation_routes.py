"""Original recommendation policy through public CF HTTP and the real D1 schema."""

import asyncio
import ast
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from test_candidate_create import env, proposal
from test_candidate_routes import api, call, new_candidate, suggested
import recommendation_kernel as kernel
from recommendation_routes import process_task_intelligence_evaluation
from internal_auth import create_request_context


@pytest.fixture
def model(env, monkeypatch):
    class Clock(datetime):
        current = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    import recommendation_routes

    async def evaluate_at_fixed_time(*args, **kwargs):
        return await kernel.evaluate(*args, **kwargs, now=Clock.current)

    monkeypatch.setattr(recommendation_routes, 'evaluate', evaluate_at_fixed_time)

    class AI:
        calls = []
        fail = False

        async def run(self, model, value):
            self.calls.append((model, value))
            if self.fail:
                raise RuntimeError('controlled provider unavailable')
            subjects = json.loads(value['messages'][1]['content'])['subjects']
            selections = [
                {
                    'subject_kind': item['subject_kind'],
                    'subject_id': item['subject_id'],
                    'why_now': 'The deadline is near.',
                    'recommended_action': 'Review the commitment.',
                }
                for item in subjects[:3]
            ]
            return {
                'choices': [
                    {
                        'finish_reason': 'stop',
                        'message': {'role': 'assistant', 'content': json.dumps({'selections': selections})},
                    }
                ],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 8},
            }

    env.AI = AI()
    env.JOBS = SimpleNamespace(messages=[])

    async def send(value):
        env.JOBS.messages.append(value)

    env.JOBS.send = send
    return env.AI, Clock


def due_candidate(api, clock, **kwargs):
    value = proposal().model_dump(mode='json')
    value['task_change']['due_at'] = (clock.current + timedelta(hours=1)).isoformat()
    return new_candidate(api, value=value, **kwargs)


def rows(env, table):
    return [dict(row) for row in env.APP_DB.connection.execute('SELECT * FROM ' + table)]


def test_real_recommendation_feedback_suppresses_all_candidate_surfaces_and_invalidates_cache(api, env, model):
    ai, clock = model
    candidate = due_candidate(api, clock)
    first = call(api, 'GET', '/v1/what-matters-now')
    assert first.status_code == 200, first.text
    recommendation = first.json()['recommendations'][0]
    assert recommendation['subject_id'] == candidate['candidate_id']
    assert recommendation['dedupe_key'] == kernel.candidate_recommendation_dedupe_key(candidate['candidate_id'])
    assert call(api, 'GET', '/v1/what-matters-now').json() == first.json() and len(ai.calls) == 1
    assert (
        len(rows(env, 'cf_task_interventions'))
        == len(rows(env, 'cf_task_evaluations'))
        == len(rows(env, 'cf_task_llm_receipts'))
        == 1
    )
    assert rows(env, 'cf_task_intelligence_jobs')[0]['status'] == 'completed'
    assert len(suggested(api)) == 1
    feedback = call(
        api,
        'POST',
        '/v1/task-intelligence/feedback',
        key='real-wmnow-later',
        json={
            'subject_kind': 'candidate',
            'subject_id': candidate['candidate_id'],
            'intervention_id': recommendation['intervention_id'],
            'action': 'later',
            'later_until': (clock.current + timedelta(minutes=5)).isoformat(),
        },
    )
    assert feedback.status_code == 200, feedback.text
    assert suggested(api) == []
    after = call(api, 'GET', '/v1/what-matters-now')
    assert after.status_code == 200 and after.json()['recommendations'] == [], after.text
    assert after.json()['material_version'] != first.json()['material_version'] and len(ai.calls) == 1
    debug_path = '/v1/task-intelligence/debug/evaluations/' + after.json()['evaluation_id']
    debug = call(api, 'GET', debug_path, request_headers={'x-omi-debug': 'true'})
    assert debug.status_code == 200 and debug.json()['decisions'][0]['reason_codes'] == ['suppressed'], debug.text
    assert call(api, 'GET', debug_path, uid='other', request_headers={'x-omi-debug': 'true'}).status_code == 404
    assert call(api, 'GET', debug_path).status_code == 404
    clock.current += timedelta(minutes=6)
    resumed = call(api, 'GET', '/v1/what-matters-now')
    assert (
        resumed.status_code == 200 and resumed.json()['recommendations'][0]['subject_id'] == candidate['candidate_id']
    ), resumed.text
    assert len(ai.calls) == 2 and len(rows(env, 'cf_task_llm_receipts')) == 2
    usage = rows(env, 'cf_llm_usage_daily')[0]
    assert usage['feature'] == 'what_matters_now' and usage['call_count'] == 2 and usage['total_tokens'] == 40


def test_ineligible_and_empty_accounts_do_not_invent_attention_or_call_model(api, env, model):
    ai, clock = model
    assert call(api, 'GET', '/v1/what-matters-now').json()['recommendations'] == []
    call(api, 'POST', '/v1/staged-tasks', json={'description': 'A low confidence staged suggestion'})
    assert call(api, 'POST', '/v1/what-matters-now/evaluate', json={}).json()['recommendations'] == []
    assert ai.calls == []
    assert call(api, 'GET', '/v1/what-matters-now', uid=None).status_code == 401
    assert call(api, 'POST', '/v1/what-matters-now/evaluate', json={'arbitrary': 'field'}).status_code == 422


def test_projection_intervention_and_completion_receipt_roll_back_together(api, env, model):
    ai, clock = model
    due_candidate(api, clock)
    env.APP_DB.connection.execute(
        "CREATE TRIGGER reject_intervention BEFORE INSERT ON cf_task_interventions BEGIN SELECT RAISE(ABORT,'publication unavailable'); END"
    )
    response = call(api, 'GET', '/v1/what-matters-now')
    assert response.status_code == 503, response.text
    for table in (
        'cf_task_interventions',
        'cf_task_evaluations',
        'cf_task_recommendation_heads',
        'cf_task_llm_receipts',
        'cf_candidate_write_guard',
    ):
        assert rows(env, table) == [], table
    assert rows(env, 'cf_task_intelligence_jobs')[0]['status'] == 'queued' and len(env.JOBS.messages) == 1


def process(env, job):
    path = '/internal/task-intelligence/evaluate'
    encoded, signature = create_request_context(
        'owner',
        env.INTERNAL_ASSERTION_SECRET,
        audience='api-core',
        method='POST',
        path=path,
        request_id='recommendation-retry',
    )

    async def body():
        return json.dumps(
            {'job_id': job['job_id'], 'account_generation': job['account_generation'], 'device_id': job['device_id']}
        ).encode()

    request = SimpleNamespace(
        scope={'env': env}, headers={'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature}, body=body
    )
    return asyncio.run(process_task_intelligence_evaluation(request))


def test_provider_failure_releases_lease_to_existing_queue_with_bounded_retry(api, env, model):
    ai, clock = model
    ai.fail = True
    due_candidate(api, clock)
    response = call(api, 'POST', '/v1/what-matters-now/evaluate', json={})
    assert response.status_code == 503, response.text
    job = rows(env, 'cf_task_intelligence_jobs')[0]
    assert job['status'] == 'queued' and job['attempts'] == 1 and len(env.JOBS.messages) == 1
    ai.fail = False
    env.APP_DB.connection.execute('UPDATE cf_task_intelligence_jobs SET next_attempt_at=0')
    response = process(env, job)
    assert response.status_code == 200, response.body
    assert rows(env, 'cf_task_intelligence_jobs')[0]['status'] == 'completed'
    assert rows(env, 'cf_task_intelligence_jobs')[0]['attempts'] == 2
    assert len(ai.calls) == 2 and len(rows(env, 'cf_task_evaluations')) == 1


def test_account_generation_change_during_model_call_cannot_publish(api, env, model):
    ai, clock = model
    due_candidate(api, clock)
    run = ai.run

    async def changing(model, value):
        result = await run(model, value)
        env.APP_DB.connection.execute(
            "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
        )
        return result

    ai.run = changing
    response = call(api, 'GET', '/v1/what-matters-now')
    assert response.status_code == 409, response.text
    assert rows(env, 'cf_task_recommendation_heads') == rows(env, 'cf_task_interventions') == []


def test_projected_messages_match_execution_of_original_message_constructor():
    source = Path(__file__).resolve().parents[5] / 'backend/utils/task_intelligence/live_recommendation_judgment.py'
    cls = next(
        node
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.ClassDef) and node.name == 'LiveRecommendationJudgment'
    )
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'judge')
    module = ast.Module(
        body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), method],
        type_ignores=[],
    )
    captured = []

    class Parser:
        def with_structured_output(self, value):
            return self

        def invoke(self, messages):
            captured.extend(messages)
            return kernel.JudgmentOutput(selections=[])

    scope = {
        'json': json,
        'cast': cast,
        'JudgmentOutput': kernel.JudgmentOutput,
        'SystemMessage': lambda **kwargs: SimpleNamespace(**kwargs),
        'HumanMessage': lambda **kwargs: SimpleNamespace(**kwargs),
    }
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), scope)
    facts = kernel.DeterministicFacts(has_concrete_next_action=True, capture_confidence=1)
    subject = SimpleNamespace(
        subject_id='test-task',
        kind=SimpleNamespace(value='task'),
        headline='Original text',
        label=None,
        evidence_preview='A real task',
        explicit_user_intent=True,
        facts=facts,
    )
    scope['judge'](SimpleNamespace(_llm_provider=lambda: Parser()), [subject])
    assert [message.content for message in captured] == [
        message['content'] for message in kernel.judgment_messages([subject])
    ]


def test_crashed_third_attempt_is_terminal_without_another_model_call(api, env, model):
    ai, clock = model
    ai.fail = True
    due_candidate(api, clock)
    assert call(api, 'GET', '/v1/what-matters-now').status_code == 503
    job = rows(env, 'cf_task_intelligence_jobs')[0]
    env.APP_DB.connection.execute(
        "UPDATE cf_task_intelligence_jobs SET status='running',attempts=3,lease_until=0,next_attempt_at=0"
    )
    ai.fail = False
    response = process(env, job)
    assert response.status_code == 503
    assert rows(env, 'cf_task_intelligence_jobs')[0]['status'] == 'failed'
    assert len(ai.calls) == 1


def test_original_subject_builder_receives_workstream_artifact_and_manual_task_state(api, env, model):
    ai, clock = model
    current = int(clock.current.timestamp())
    db = env.APP_DB.connection
    db.execute(
        "INSERT INTO cf_action_items(uid,id,description,status,owner,source,created_at,updated_at) VALUES ('owner','manual','Manually requested work','active','user','manual',?,?)",
        (current, current),
    )
    db.execute(
        "INSERT INTO cf_workstreams(uid,id,title,objective,status,next_review_at,created_at,updated_at) VALUES ('owner','stream','Launch work','Ship it','open',?,?,?)",
        (current, current, current),
    )
    db.execute(
        "INSERT INTO cf_workstream_events(uid,event_id,workstream_id,sequence,kind,summary,evidence_refs_json,sensitivity,created_at) VALUES ('owner','event','stream',1,'decision','Review milestone','[]','normal',?)",
        (current,),
    )
    db.execute(
        "INSERT INTO cf_workstream_artifacts(uid,artifact_id,workstream_id,logical_key,version,kind,uri,content_hash,evidence_refs_json,status,created_at) VALUES ('owner','artifact','stream','release',1,'document','r2://synthetic/release','1234567890abcdef',?,'awaiting_review',?)",
        (json.dumps([{'kind': 'external', 'id': 'review-proof', 'scope': 'canonical'}]), current),
    )
    response = call(api, 'GET', '/v1/what-matters-now')
    assert response.status_code == 200, response.text
    supplied = json.loads(ai.calls[0][1]['messages'][1]['content'])['subjects']
    assert {item['subject_kind'] for item in supplied} == {'task', 'workstream', 'artifact'}
    assert {item['subject_kind'] for item in response.json()['recommendations']} == {'task', 'workstream', 'artifact'}
    assert call(api, 'GET', '/v1/what-matters-now', uid='other').json()['recommendations'] == []


@pytest.mark.parametrize('fenced_uid', ['owner', 'other'])
def test_recommendation_head_cannot_move_out_of_or_into_a_deleted_account(api, env, model, fenced_uid):
    # A previously admitted write must obey both OLD and NEW owner fences.
    ai, clock = model
    due_candidate(api, clock)
    assert call(api, 'GET', '/v1/what-matters-now').status_code == 200
    before = rows(env, 'cf_task_recommendation_heads')
    head_id = before[0]['head_id']
    db = env.APP_DB.connection
    db.execute(
        'INSERT INTO cf_candidate_write_guard(uid,account_generation,recommendations_json) VALUES (?,0,?)',
        ('other', json.dumps([{'id': head_id, 'before': None}])),
    )
    db.execute('INSERT INTO cf_account_deletion_tombstones VALUES (?,1,9999999999)', (fenced_uid,))
    with pytest.raises(sqlite3.IntegrityError, match='account deletion fence'):
        db.execute(
            'UPDATE cf_task_recommendation_heads SET uid=? WHERE uid=? AND head_id=?', ('other', 'owner', head_id)
        )
    assert rows(env, 'cf_task_recommendation_heads') == before
