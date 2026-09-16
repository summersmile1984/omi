"""Hosted OpenAI-compatible chat adapter using the existing LangChain owners."""

import httpx
from langchain_openai import ChatOpenAI

from . import operator_ai
from .egress_policy import assert_http_endpoint_allowed


class HostedChat(ChatOpenAI):
    def with_structured_output(self, schema=None, *, method=None, **kwargs):
        from langchain_core.runnables import RunnableLambda

        chain = super().with_structured_output(schema, method='function_calling', **kwargs)

        def require_result(value):
            if value is None:
                raise ValueError('the hosted model did not return the requested structured result')
            return value

        return chain | RunnableLambda(require_result)


def build(streaming=False, options=None):
    from utils.llm.usage_tracker import get_usage_callback
    from .llm_usage import UsageCallback

    selected = operator_ai.current()
    if not isinstance(selected, operator_ai.HostedOperatorAI):
        raise ValueError('hosted chat requires an OpenAI-compatible operator selection')

    def guard(request):
        assert_http_endpoint_allowed(str(request.url))

    async def aguard(request):
        guard(request)

    options = options or {}
    return HostedChat(
        model=selected.model,
        api_key=operator_ai.credentials(),
        base_url=selected.base_url,
        timeout=selected.request_timeout_seconds,
        max_retries=0,
        max_tokens=selected.max_output_tokens,
        temperature=0,
        streaming=streaming,
        stream_usage=True,
        model_kwargs={'response_format': {'type': 'json_object'}} if options.get('response_schema') else {},
        default_headers=operator_ai.gateway_headers(),
        http_client=httpx.Client(follow_redirects=False, event_hooks={'request': [guard]}),
        http_async_client=httpx.AsyncClient(follow_redirects=False, event_hooks={'request': [aguard]}),
        callbacks=[UsageCallback(get_usage_callback(), selected.model)],
    )
