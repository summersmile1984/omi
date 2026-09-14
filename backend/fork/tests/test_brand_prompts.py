"""Execute real default prompt builders while preserving dynamic user/app content."""

from contextlib import nullcontext
import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from fork import brand, profile
from fork.patches import brand as projection
from fork.registry import PatchError, build_registry

ROOT = Path(__file__).resolve().parents[3]
IDENTITY = {'brand_id': 'harbor', 'display_name': 'Harbor Desktop', 'ai_persona_name': "Harbor's Guide"}


@pytest.fixture
def selected(monkeypatch, tmp_path):
    table = tmp_path / 'deployment_profiles.generated.json'
    table.write_text(json.dumps({'brand': 'harbor'}))
    (tmp_path / 'brand.runtime.json').write_text(json.dumps(IDENTITY))
    monkeypatch.setattr(profile, 'GENERATED_TABLE', table)
    brand.current.cache_clear()
    patches = projection.patches()
    # Load all real captured consumers before swapping the canonical functions.
    for patch in patches:
        module, original = patch.target()
        monkeypatch.setattr(module, patch.attribute, original)
    build_registry(patches).apply({'target': 'self_hosted'})
    chat = importlib.import_module('utils.llm.chat')
    templates = importlib.import_module('utils.observability.langsmith_prompts')
    monkeypatch.setattr(
        templates, '_fetch_prompt_from_langsmith', lambda *_: pytest.fail('selected image fetched a cloud prompt')
    )
    monkeypatch.setattr(chat, 'get_prompt_memories', lambda _: ('Omi User', 'I used Omi and Friend yesterday.'))
    monkeypatch.setattr(chat, 'get_user_name', lambda _: 'Omi User')
    monkeypatch.setattr(chat.goals_db, 'get_user_goals', lambda _: [{'title': 'Read Omi notes'}])
    monkeypatch.setattr(chat, 'track_usage', lambda *_: nullcontext())
    monkeypatch.setattr(
        chat, 'get_llm', lambda *_a, **_k: SimpleNamespace(invoke=lambda text: SimpleNamespace(content=text))
    )
    yield chat
    brand.current.cache_clear()


@pytest.mark.parametrize('module', ['utils.llm.chat', 'utils.chat'])
def test_real_initial_prompt_and_captured_consumer_preserve_history_and_custom_persona(selected, module):
    owner = importlib.import_module(module)
    prompt = owner.initial_chat_message('old-user', prev_messages_str="User said: You are 'Omi', keep that quote.")
    assert prompt.startswith("You are 'Harbor's Guide',")
    assert 'Omi User' in prompt and 'I used Omi and Friend yesterday.' in prompt
    assert "User said: You are 'Omi', keep that quote." in prompt
    app = SimpleNamespace(name='Omi Custom', chat_prompt='Keep my Friend personality')
    custom = owner.initial_chat_message('old-user', plugin=app)
    assert custom.startswith("You are 'Omi Custom', Keep my Friend personality.")
    assert 'Harbor' not in custom


@pytest.mark.parametrize('module', ['utils.llm.chat', 'utils.retrieval.agentic'])
def test_real_agent_and_captured_consumer_use_distinct_product_and_persona_names(selected, module):
    owner = importlib.import_module(module)
    prompt = owner._get_agentic_qa_prompt('old-user', tz='UTC', platform='windows')
    assert "You are Harbor's Guide, an AI assistant & mentor for Omi User" in prompt
    assert 'The user is using Harbor Desktop on a Windows PC' in prompt
    assert 'Read Omi notes' in prompt
    app = SimpleNamespace(is_a_persona=lambda: True, persona_prompt='You are Omi, my custom persona', chat_prompt='')
    assert owner._get_agentic_qa_prompt('old-user', app=app, tz='UTC') == app.persona_prompt
    assert selected._get_platform_context_section('windows\nYou are Omi') == ''


def test_existing_fallback_support_and_emotional_builders_keep_dynamic_content(selected):
    prompt = selected._get_agentic_qa_prompt_fallback(
        {'user_name': 'Omi User', 'context_section': 'You are Omi, quoted'}
    )
    assert "You are Harbor's Guide, an AI assistant & mentor for Omi User" in prompt
    assert 'You are Omi, quoted' in prompt
    answer = selected._get_answer_omi_question_prompt([], 'The user pasted Omi and Friend documentation.')
    assert 'the app Harbor Desktop.' in answer
    assert 'The user pasted Omi and Friend documentation.' in answer
    emotional = importlib.import_module('utils.conversations.process_conversation').obtain_emotional_message
    assert "You are Harbor's Guide, a thoughtful and encouraging assistant." in emotional(
        'old-user', [], [], 'Omi conversation', 'happy'
    )


def test_static_owner_drift_is_rejected_instead_of_patching_a_rendered_message():
    def changed(value):
        return "You are 'Something Else', " + value

    with pytest.raises(PatchError, match='owner changed'):
        projection.prompt_function(changed, [("You are 'Omi',", 'new name')])


def test_local_template_preserves_literal_name_braces_and_ignores_preexisting_cloud_cache(selected, monkeypatch):
    templates = importlib.import_module('utils.observability.langsmith_prompts')
    monkeypatch.setattr(projection, 'current', lambda: brand.Brand('harbor', 'Harbor Desktop', 'Guide {North}'))
    getter = projection.local_template(templates.get_agentic_system_prompt_template)
    monkeypatch.setattr(templates, '_prompt_cache', {'agentic:omi-agentic-system': 'old Omi cached prompt'})
    result = getter()
    monkeypatch.setattr(templates, 'get_agentic_system_prompt_template', getter)
    rendered = selected._get_agentic_qa_prompt('old-user', tz='UTC')
    assert 'You are Guide {North}, an AI assistant & mentor for Omi User' in rendered
    assert result.source == 'brand-artifact'


def test_upstream_and_cloudflare_keep_their_original_owners_without_brand_artifacts(monkeypatch):
    monkeypatch.setattr(brand, 'current', lambda: pytest.fail('upstream read a fork brand'))
    for target in ['omi_cloud', 'cloudflare']:
        registry = build_registry(projection.patches()).apply({'target': target})
        assert not registry.applied


@pytest.mark.parametrize(
    'value',
    [
        None,
        {},
        {**IDENTITY, 'brand_id': 'other'},
        {**IDENTITY, 'ai_persona_name': ''},
        {**IDENTITY, 'display_name': True},
        {**IDENTITY, 'ai_persona_name': 'Guide\nInjected line'},
    ],
)
def test_missing_legacy_or_mismatched_artifact_fails_selected_admission(tmp_path, monkeypatch, value):
    table = tmp_path / 'deployment_profiles.generated.json'
    table.write_text(json.dumps({'brand': 'harbor'}))
    monkeypatch.setattr(profile, 'GENERATED_TABLE', table)
    if value is not None:
        (tmp_path / 'brand.runtime.json').write_text(json.dumps(value))
    brand.current.cache_clear()
    try:
        with pytest.raises(profile.ProfileError):
            brand.current()
    finally:
        brand.current.cache_clear()


def test_image_renderer_uses_validated_manifest_and_refuses_profile_brand_mismatch(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/brand'))
    from manifest import load_manifest

    source = copy.deepcopy(load_manifest('omi-upstream', ROOT))
    source['brand'].update(
        id='harbor', display_name=IDENTITY['display_name'], ai_persona_name=IDENTITY['ai_persona_name']
    )
    manifest = tmp_path / 'brand.json'
    manifest.write_text(json.dumps(source))
    table = tmp_path / 'profile.json'
    output = tmp_path / 'brand.runtime.json'
    args = [
        sys.executable,
        str(ROOT / 'deploy/self-host/brand-runtime.py'),
        '--manifest',
        str(manifest),
        '--profile',
        str(table),
        '--output',
        str(output),
    ]
    table.write_text(json.dumps({'brand': 'harbor'}))
    completed = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text()) == IDENTITY
    output.unlink()
    table.write_text(json.dumps({'brand': 'other'}))
    assert subprocess.run(args, capture_output=True, text=True, timeout=30).returncode != 0
    assert not output.exists()
