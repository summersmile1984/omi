"""Bounded direct-provider fallback shared by model-neutral deployments.

Only transport timeout, connection, 429, and 5xx failures are eligible. Auth,
validation, capability, and application failures remain terminal so a fallback
cannot silently change the requested model contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import httpx
from langchain_core.callbacks.manager import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig

from utils.observability.fallback import record_fallback


def direct_fallback_reason(error: BaseException) -> str | None:
    if isinstance(error, (TimeoutError, httpx.TimeoutException, httpx.ConnectError)):
        return 'timeout'
    status = getattr(error, 'status_code', None)
    response = getattr(error, 'response', None)
    if not isinstance(status, int) and response is not None:
        status = getattr(response, 'status_code', None)
    if status == 429:
        return 'provider_429'
    if isinstance(status, int) and status >= 500:
        return 'provider_5xx'
    name = type(error).__name__.casefold()
    if 'timeout' in name or 'connectionerror' in name:
        return 'timeout'
    return None


def _record(from_route: str, to_route: str, reason: str | None, outcome: str) -> None:
    record_fallback(
        component='llm_gateway',
        from_mode=from_route,
        to_mode=to_route,
        reason=reason or 'other',
        outcome=outcome,
    )


class BoundedFallbackRunnable(Runnable[Any, Any]):
    def __init__(self, candidates: Sequence[Runnable[Any, Any]], labels: Sequence[str], *, feature: str) -> None:
        if not candidates or len(candidates) != len(labels):
            raise ValueError('fallback candidates and labels must be non-empty and aligned')
        self._candidates = tuple(candidates)
        self._labels = tuple(labels)
        self._feature = feature

    def invoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        first_reason: str | None = None
        for index, candidate in enumerate(self._candidates):
            try:
                result = candidate.invoke(input, config=config, **kwargs)
                if index:
                    _record(self._labels[0], self._labels[index], first_reason, 'recovered')
                return result
            except BaseException as error:
                reason = direct_fallback_reason(error)
                if reason is None or index == len(self._candidates) - 1:
                    if first_reason:
                        _record(self._labels[0], 'none', first_reason, 'exhausted')
                    raise
                first_reason = first_reason or reason
        raise RuntimeError('fallback candidates exhausted')

    async def ainvoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        first_reason: str | None = None
        for index, candidate in enumerate(self._candidates):
            try:
                result = await candidate.ainvoke(input, config=config, **kwargs)
                if index:
                    _record(self._labels[0], self._labels[index], first_reason, 'recovered')
                return result
            except BaseException as error:
                reason = direct_fallback_reason(error)
                if reason is None or index == len(self._candidates) - 1:
                    if first_reason:
                        _record(self._labels[0], 'none', first_reason, 'exhausted')
                    raise
                first_reason = first_reason or reason
        raise RuntimeError('fallback candidates exhausted')

    def stream(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Iterator[Any]:
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
                    raise
                first_reason = first_reason or reason
                continue
            if index:
                _record(self._labels[0], self._labels[index], first_reason, 'recovered')
            yield first
            yield from iterator
            return

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
                    raise
                first_reason = first_reason or reason
                continue
            if index:
                _record(self._labels[0], self._labels[index], first_reason, 'recovered')
            yield first
            async for chunk in iterator:
                yield chunk
            return


class BoundedFallbackChatModel(BaseChatModel):
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
        for index, candidate in enumerate(self._candidates()):
            try:
                return candidate._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            except BaseException as error:
                reason = direct_fallback_reason(error)
                if reason is None or index == len(self._candidates()) - 1:
                    raise
                if index == 0:
                    _record(self.route_labels[0], self.route_labels[index + 1], reason, 'recovered')
        raise RuntimeError('fallback candidates exhausted')

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        for index, candidate in enumerate(self._candidates()):
            try:
                return await candidate._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
            except BaseException as error:
                reason = direct_fallback_reason(error)
                if reason is None or index == len(self._candidates()) - 1:
                    raise
                if index == 0:
                    _record(self.route_labels[0], self.route_labels[index + 1], reason, 'recovered')
        raise RuntimeError('fallback candidates exhausted')

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        for index, candidate in enumerate(self._candidates()):
            iterator = candidate._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            try:
                first = next(iterator)
            except StopIteration:
                return
            except BaseException as error:
                reason = direct_fallback_reason(error)
                if reason is None or index == len(self._candidates()) - 1:
                    raise
                if index == 0:
                    _record(self.route_labels[0], self.route_labels[index + 1], reason, 'recovered')
                continue
            yield first
            yield from iterator
            return

    def with_structured_output(
        self, schema: dict[str, Any] | type, *, include_raw: bool = False, **kwargs: Any
    ) -> Runnable[Any, Any]:
        candidates = [
            candidate.with_structured_output(schema, include_raw=include_raw, **kwargs)
            for candidate in self._candidates()
        ]
        return BoundedFallbackRunnable(candidates, self.route_labels, feature=self.feature)
