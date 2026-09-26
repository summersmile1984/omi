"""Project every self-hosted text feature onto the profile's operator AI.

The retired ``patches/llm.py`` bound these same upstream seams to the local
Ollama runtime. That runtime is gone and ``fork.bootstrap`` refuses a
``self_hosted`` row carrying a local ``llm``, so the only admitted text
identity is the profile's ``operator_ai`` selection (``fork.operator_ai``
freezes the vendor endpoints, model, credential env and egress grants).
Without these bindings upstream's Model QoS profile would keep resolving
cloud vendors this deployment has no credentials for — silently, at request
time, which is the failure class ``fork.registry`` exists to prevent.
"""

from __future__ import annotations

from typing import Any

from ..registry import Patch


class LLMInputRejected(ValueError):
    """Input or caller state is outside the selected operator's contract."""

    status_code = 422


class LLMUnavailable(RuntimeError):
    """This deployment admits no text model for the requested route."""

    status_code = 503


# The real user-scoped PG/vector handlers stay. Cloud app, web search,
# frame/vision and external calendar tools are not capabilities of this model.
TOOLS = frozenset(
    {
        'get_conversations_tool',
        'search_conversations_tool',
        'get_memories_tool',
        'search_memories_tool',
        'get_action_items_tool',
        'create_action_item_tool',
        'update_action_item_tool',
        'save_user_preference_tool',
    }
)


def selected_agent(original):
    async def run(
        system_prompt, messages, tool_schemas, tool_registry, callback, full_response, safety_guard, configurable
    ):
        registry = {name: tool for name, tool in tool_registry.items() if name in TOOLS}
        schemas = [schema for schema in tool_schemas if schema.get('function', {}).get('name') in registry]
        prompt = system_prompt + (
            '\n<deployment_capabilities>Only the tools in this request are available. '
            'This deployment has no web search, external app integrations or image understanding. '
            'Use the available conversation and memory tools for personal history. '
            'If the needed capability is absent, explain that limitation.</deployment_capabilities>'
        )
        return await original(prompt, messages, schemas, registry, callback, full_response, safety_guard, configurable)

    return run


def _contract():
    from .. import operator_ai, profile

    # select() (not operator_ai.current()) so an unselected deployment can
    # resolve to the disabled identity instead of raising at model-config time.
    return operator_ai.select(profile.current())


def route(feature: str) -> tuple[str, str]:
    """The admitted (model, provider) identity for one configured feature."""
    from utils.byok import has_byok_keys
    from utils.llm.model_config import get_all_configured_features

    if feature not in get_all_configured_features():
        raise LLMInputRejected('unknown LLM feature is not an admitted product route')
    if has_byok_keys():
        raise LLMInputRejected('BYOK is disabled by the selected self-host model contract')
    contract = _contract()
    return (contract.model, contract.provider) if contract else ('disabled', 'disabled')


def build(model: str, provider: str, streaming: bool = False, options: dict[str, Any] | None = None):
    """Construct the admitted operator's chat client; no vendor fallback exists."""
    contract = _contract()
    if contract is None:
        raise LLMUnavailable('this deployment admits no text model')
    if (model, provider) != (contract.model, contract.provider):
        raise LLMInputRejected('caller changed the selected text model')
    if contract.provider == 'mimo':
        from ..mimo_chat import build as build_mimo

        return build_mimo(streaming=streaming, options=options)
    from ..operator_chat import build as build_hosted

    return build_hosted(streaming=streaming, options=options)


def route_options(feature: str, model: str, provider: str) -> dict[str, Any]:
    if (model, provider) != route(feature):
        raise LLMInputRejected('feature options changed the selected model')
    if feature == 'memory_l1':
        # The existing extractor supplies this exact parser contract in its
        # prompt. Constrain generation to it; retain the original parser,
        # attribution, archive policy and canonical persistence owners.
        from utils.llm.working_observations import WorkingObservationBatch

        return {'response_schema': WorkingObservationBatch.model_json_schema()}
    return {}


def patches() -> list[Patch]:
    # Re-derived from the current upstream symbols during the 2026-09-26
    # sync: utils.llm.clients still re-exports the imported names it calls
    # through, so both the defining module and the consumer module bind.
    targets = [
        ('utils.llm.model_config', '_get_model_config', lambda _: route),
        ('utils.llm.clients', '_get_model_config', lambda _: route),
        ('utils.llm.model_config', 'get_route_options', lambda _: route_options),
        ('utils.llm.clients', 'get_route_options', lambda _: route_options),
        ('utils.llm.providers', 'get_default_client', lambda _: build),
        ('utils.llm.clients', 'get_default_client', lambda _: build),
        ('utils.retrieval.agentic', '_run_openai_agent_stream', selected_agent),
    ]
    return [
        Patch(
            name='operator_llm.' + module + '.' + attribute,
            module=module,
            attribute=attribute,
            build=build,
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='text features share the admitted operator identity; no cloud credentials or vendor fallback',
        )
        for module, attribute, build in targets
    ]
