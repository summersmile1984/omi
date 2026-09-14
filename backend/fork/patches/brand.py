"""Project reviewed static prompt literals without rewriting user or plugin data."""

from functools import update_wrapper
from hashlib import sha256
import time
from types import FunctionType

from ..brand import current
from ..registry import Patch, PatchError


def prompt_function(original, replacements):
    """Retain production code, globals, defaults and dynamic interpolation.

    Only this function's compiled string constants are projected. Rewriting a
    rendered prompt would also corrupt user history and custom app personalities.
    Exact static fragment counts are a drift tripwire; behavioral tests execute
    the original prompt builders and their captured production consumers.
    """
    constants = list(original.__code__.co_consts)
    for before, after in replacements:
        count = sum(value.count(before) for value in constants if isinstance(value, str))
        if count != 1:
            raise PatchError(f'brand prompt owner changed: {original.__module__}.{original.__name__}')
        constants = [value.replace(before, after) if isinstance(value, str) else value for value in constants]
    clone = FunctionType(
        original.__code__.replace(co_consts=tuple(constants)),
        original.__globals__,
        original.__name__,
        original.__defaults__,
        original.__closure__,
    )
    clone.__kwdefaults__ = original.__kwdefaults__
    return update_wrapper(clone, original)


def initial(original):
    return prompt_function(original, [("You are 'Omi',", f"You are '{current().ai_persona_name}',")])


def agent(original):
    return prompt_function(
        original,
        [('You are Omi, an AI assistant & mentor', f'You are {current().ai_persona_name}, an AI assistant & mentor')],
    )


def product_answer(original):
    return prompt_function(original, [('the app Omi, also known as Friend.', f'the app {current().display_name}.')])


def emotional(original):
    return prompt_function(
        original,
        [
            (
                'You are a thoughtful and encouraging Friend.',
                f'You are {current().ai_persona_name}, a thoughtful and encouraging assistant.',
            )
        ],
    )


def platforms(original):
    prefix = 'The user is using Omi on '
    if set(original) != {'windows', 'macos', 'ios', 'android'} or any(
        not isinstance(value, str) or value.count(prefix) != 1 or not value.startswith(prefix)
        for value in original.values()
    ):
        raise PatchError('brand platform prompt owner changed')
    return {
        key: value.replace(prefix, f'The user is using {current().display_name} on ', 1)
        for key, value in original.items()
    }


def local_template(original):
    # The selected image owns its default prompt. The real upstream rendering
    # path also consults a LangSmith template, even when its local API key is
    # absent; selecting only the inline fallback leaves that primary path Omi.
    # Preserve the canonical local template and render_prompt interpolation.
    from utils.observability.langsmith_prompts import CachedPrompt, _get_fallback_agentic_prompt_template

    persona = current().ai_persona_name.replace('{', '{{').replace('}', '}}')
    template = prompt_function(
        _get_fallback_agentic_prompt_template,
        [('You are Omi, an AI assistant & mentor', f'You are {persona}, an AI assistant & mentor')],
    )()
    digest = sha256(template.encode()).hexdigest()
    created_at = time.time()

    def selected():
        return CachedPrompt(template, 'brand-agentic-system', digest, created_at, 'brand-artifact')

    return selected


def patches():
    targets = [
        ('utils.llm.chat', 'initial_chat_message', initial),
        ('utils.chat', 'initial_chat_message', initial),
        ('utils.llm.chat', '_get_agentic_qa_prompt', agent),
        ('utils.retrieval.agentic', '_get_agentic_qa_prompt', agent),
        ('utils.llm.chat', '_get_agentic_qa_prompt_fallback', agent),
        ('utils.llm.chat', '_get_answer_omi_question_prompt', product_answer),
        ('utils.llm.chat', 'obtain_emotional_message', emotional),
        ('utils.conversations.process_conversation', 'obtain_emotional_message', emotional),
        ('utils.llm.chat', '_PLATFORM_CONTEXT_LINES', platforms),
        ('utils.observability.langsmith_prompts', 'get_agentic_system_prompt_template', local_template),
    ]
    return [
        Patch(
            name='brand.' + module + '.' + attribute,
            module=module,
            attribute=attribute,
            build=build,
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='selected default assistant and product names come from the image brand; user/plugin text stays original',
        )
        for module, attribute, build in targets
    ]
