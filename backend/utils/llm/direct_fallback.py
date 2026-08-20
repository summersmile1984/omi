"""Bounded direct-mode fallback with the same failure policy as the gateway.

Fallback is attempted only for transport timeouts, provider 429 responses, and
provider 5xx responses. Authentication, validation, parsing, and arbitrary
application errors fail closed on the selected route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import httpx
from langchain_core.callbacks.manager import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig

from utils.observability.fallback import record_fallback


def direct_fallback_reason(error: BaseException) -> str | None:
    """Return the shared bounded reason when a direct provider may fail over."""

    if isinstance(error, (TimeoutError, httpx.TimeoutException, httpx.ConnectError)):
        return 'timeout'
    status_code = getattr(error, 'status_code', None)
    if not isinstance(status_code, int):
        response = getattr(error, 'response', None)
        status_code = getattr(response, 'status_code', None)
    if status_code == 429:
        return 'provider_429'
    if isinstance(status_code, int) and status_code >= 500:
        return 'provider_5xx'
    class_name = type(error).__name__.casefold()
    if 'timeout' in class_name or 'connectionerror' in class_name:
        return 'timeout'
    return None


class BoundedFallbackRunnable(Runnable[Any, Any]):
    """Apply the bounded provider policy to structured/bound runnable calls."""

    def __init__(self, candidates: Sequence[Runnable[Any, Any]], labels: Sequence[str], *, feature: str) -> None:
        if not candidates or len(candidates) != len(labels):
            raise ValueError('fallback runnable candidates and labels must be non-empty and aligned')
        self._candidates = tuple(candidates)
        self._labels = tuple(labels)
        self._feature = feature

    def invoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        return self._invoke_candidates(input, config, **kwargs)

    async def ainvoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        last_error: BaseException | None = None
        first_reason: str | None = None
        for index, candidate in enumerate(self._candidates):
            try:
                result = await candidate.ainvoke(input, config=config, **kwargs)
                if index:
                    _record_route_fallback(self._labels[0], self._labels[index], first_reason, 'recovered')
                return result
            except BaseException as error:
                reason = direct_fallback_reason(error)
                if reason is None or index == len(self._candidates) - 1:
                    if first_reason is not None:
                        _record_route_fallback(self._labels[0], 'none', first_reason, 'exhausted')
                    raise
                first_reason = first_reason or reason
                last_error = error
        raise RuntimeError('bounded fallback runnable exhausted without a result') from last_error

    def stream(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Iterator[Any]:
        yield from self._stream_candidates(input, config, **kwargs)

    async def astream(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> AsyncIterator[Any]:
        first_reason: str | None = None
        for index, candidate in enumerate(self._candidates):
            iterator = candidate.astream(input, config=config, **kwargs)
            try:
                first = await anext(iterator)
            except StopAsyncIteration:
                return
            except BaseException as error:
                reason = direct_fallback_reason(error)
                if reason is None or index == len(self._candidates) - 1:
                    if first_reason is not None:
                        _record_route_fallback(self._labels[0], 'none', first_reason, 'exhausted')
                    raise
                first_reason = first_reason or reason
                continue
            if index:
                _record_route_fallback(self._labels[0], self._labels[index], first_reason, 'recovered')
            yield first
            async for chunk in iterator:
                yield chunk
            return

    def _invoke_candidates(self, input: Any, config: RunnableConfig | None, **kwargs: Any) -> Any:
        last_error: BaseException | None = None
        first_reason: str | None = None
        for index, candidate in enumerate(self._candidates):
            try:
                result = candidate.invoke(input, config=config, **kwargs)
                if index:
                    _record_route_fallback(self._labels[0], self._labels[index], first_reason, 'recovered')
                return result
            except BaseException as error:
                reason = direct_fallback_reason(error)
                if reason is None or index == len(self._candidates) - 1:
                    if first_reason is not None:
                        _record_route_fallback(self._labels[0], 'none', first_reason, 'exhausted')
                    raise
                first_reason = first_reason or reason
                last_error = error
        raise RuntimeError('bounded fallback runnable exhausted without a result') from last_error

    def _stream_candidates(self, input: Any, config: RunnableConfig | None, **kwargs: Any) -> Iterator[Any]:
        first_reason: str | None = None
        for index, candidate in enumerate(self._candidates):
            iterator = candidate.stream(input, config=config, **kwargs)
            try:
                first = next(iterator)
            except StopIteration:
                return
            except BaseException as error:
                reason = direct_fallback_reason(error)
                if reason is None or index == len(self._candidates) - 1:
                    if first_reason is not None:
                        _record_route_fallback(self._labels[0], 'none', first_reason, 'exhausted')
                    raise
                first_reason = first_reason or reason
                continue
            if index:
                _record_route_fallback(self._labels[0], self._labels[index], first_reason, 'recovered')
            yield first
            yield from iterator
            return


class BoundedFallbackChatModel(BaseChatModel):
    """BaseChatModel preserving LCEL and structured-output behavior."""

    primary: BaseChatModel
    fallback_models: tuple[BaseChatModel, ...]
    route_labels: tuple[str, ...]
    feature: str

    @property
    def _llm_type(self) -> str:
        return 'omi-bounded-direct-fallback'

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {'feature': self.feature, 'routes': list(self.route_labels)}

    def _candidates(self) -> tuple[BaseChatModel, ...]:
        return (self.primary, *self.fallback_models)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        runnable = BoundedFallbackRunnable(self._candidates(), self.route_labels, feature=self.feature)
        message = runnable.invoke(messages, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        runnable = BoundedFallbackRunnable(self._candidates(), self.route_labels, feature=self.feature)
        message = await runnable.ainvoke(messages, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        runnable = BoundedFallbackRunnable(self._candidates(), self.route_labels, feature=self.feature)
        for message in runnable.stream(messages, stop=stop, **kwargs):
            yield ChatGenerationChunk(message=message)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        runnable = BoundedFallbackRunnable(self._candidates(), self.route_labels, feature=self.feature)
        async for message in runnable.astream(messages, stop=stop, **kwargs):
            yield ChatGenerationChunk(message=message)

    def with_structured_output(
        self,
        schema: dict[str, Any] | type,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        candidates = [
            candidate.with_structured_output(schema, include_raw=include_raw, **kwargs)
            for candidate in self._candidates()
        ]
        return BoundedFallbackRunnable(candidates, self.route_labels, feature=self.feature)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | dict[str, Any] | bool | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        """Preserve the bounded route policy for OpenAI-compatible tool calls."""

        bind_options = dict(kwargs)
        if tool_choice is not None:
            bind_options['tool_choice'] = tool_choice
        candidates = [candidate.bind_tools(tools, **bind_options) for candidate in self._candidates()]
        return BoundedFallbackRunnable(candidates, self.route_labels, feature=self.feature)


def _record_route_fallback(from_mode: str, to_mode: str, reason: str | None, outcome: str) -> None:
    record_fallback(
        component='llm_gateway',
        from_mode=from_mode,
        to_mode=to_mode,
        reason=reason or 'other',
        outcome=outcome,
    )
