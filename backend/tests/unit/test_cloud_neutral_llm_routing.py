"""Regression tests for explicit operator-owned model routes."""

import pytest
import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from utils.llm import model_config
from utils.llm.direct_fallback import BoundedFallbackChatModel


class _SequencedModel(BaseChatModel):
    name: str
    outcome: object

    @property
    def _llm_type(self) -> str:
        return self.name

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=str(self.outcome)))])


def test_neutral_deployment_rejects_checked_in_vendor_profile() -> None:
    with pytest.raises(ValueError, match='explicit provider/model route'):
        model_config.resolve_feature_route('chat_responses', {'OMI_DEPLOYMENT_PROFILE': 'self_hosted'})


def test_neutral_deployment_accepts_explicit_generic_route_and_fallback() -> None:
    route = model_config.resolve_feature_route(
        'chat_responses',
        {
            'OMI_DEPLOYMENT_PROFILE': 'self_hosted',
            'OMI_LLM_DEFAULT_PROVIDER': 'generic',
            'OMI_LLM_DEFAULT_MODEL': 'local-chat',
            'OMI_LLM_DEFAULT_FALLBACKS': 'generic:local-chat-backup',
        },
    )

    assert route.primary == model_config.ProviderRoute(provider='generic', model='local-chat')
    assert route.fallbacks == (model_config.ProviderRoute(provider='generic', model='local-chat-backup'),)


def test_neutral_provider_without_model_does_not_use_ambient_provider_defaults() -> None:
    with pytest.raises(ValueError, match='explicit provider/model route'):
        model_config.resolve_feature_route(
            'chat_responses',
            {
                'OMI_DEPLOYMENT_PROFILE': 'neutral',
                'OMI_LLM_DEFAULT_PROVIDER': 'deepseek',
                'DEEPSEEK_MODEL': '',
            },
        )


def test_direct_route_fallback_only_recovers_transport_failures() -> None:
    primary = _SequencedModel(name='primary', outcome=httpx.ReadTimeout('timed out'))
    fallback = _SequencedModel(name='fallback', outcome='operator response')
    result = BoundedFallbackChatModel(
        primary=primary,
        fallback_models=(fallback,),
        route_labels=('generic:primary', 'generic:fallback'),
        feature='chat_responses',
    ).invoke('hello')
    assert result.content == 'operator response'

    rejected = _SequencedModel(name='rejected', outcome=ValueError('invalid request'))
    untouched = _SequencedModel(name='untouched', outcome='must not run')
    with pytest.raises(ValueError, match='invalid request'):
        BoundedFallbackChatModel(
            primary=rejected,
            fallback_models=(untouched,),
            route_labels=('generic:rejected', 'generic:untouched'),
            feature='chat_responses',
        ).invoke('hello')
