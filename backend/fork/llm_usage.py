"""Feed completed native token usage into the existing billing callback."""

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


class UsageCallback(BaseCallbackHandler):
    def __init__(self, delegate, model):
        self.delegate, self.model = delegate, model

    def on_llm_end(self, response, **kwargs):
        # LangChain streaming aggregates message usage, while the existing
        # repository callback reads llm_output. Only a terminal native event
        # supplies usage; failed/cancelled streams never call on_llm_end.
        usage = [
            getattr(generation.message, 'usage_metadata', None)
            for group in response.generations
            for generation in group
        ]
        usage = [item for item in usage if item]
        if not usage:
            return
        self.delegate.on_llm_end(
            LLMResult(
                generations=response.generations,
                llm_output={
                    'model_name': self.model,
                    'token_usage': {
                        'prompt_tokens': sum(item['input_tokens'] for item in usage),
                        'completion_tokens': sum(item['output_tokens'] for item in usage),
                    },
                },
            ),
            **kwargs
        )
