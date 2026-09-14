"""Project text-model factories and the existing agent loop onto one authority."""

from ..registry import Patch

# These retain the real user-scoped PG/vector handlers. Cloud app, web search,
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


def patches():
    from .. import local_llm

    targets = [
        ('utils.llm.model_config', '_get_model_config', lambda _: local_llm.route),
        ('utils.llm.clients', '_get_model_config', lambda _: local_llm.route),
        ('utils.llm.model_config', 'get_route_options', lambda _: local_llm.route_options),
        ('utils.llm.clients', 'get_route_options', lambda _: local_llm.route_options),
        ('utils.llm.providers', 'get_default_client', lambda _: local_llm.build),
        ('utils.llm.clients', 'get_default_client', lambda _: local_llm.build),
        ('utils.retrieval.agentic', '_run_openai_agent_stream', selected_agent),
    ]
    return [
        Patch(
            name='llm.' + module + '.' + attribute,
            module=module,
            attribute=attribute,
            build=build,
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='text features share selected model identity; no cloud credentials, gateway or tool fallback',
        )
        for module, attribute, build in targets
    ]
