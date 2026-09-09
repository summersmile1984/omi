"""Scope monotonic wall time to a canonical operation's state transition."""

from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from types import FunctionType


def wall_now(tz):
    return datetime.now(tz)


def install_operation_clock(model):
    original = model._transition
    floor = ContextVar('memory_operation_clock_floor', default=None)

    class TransitionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return max(floor.get(), wall_now(tz))

    # Preserve the upstream transition body, including terminal-state and
    # payload validation. Only its clock dependency changes; imports elsewhere
    # and persisted-record decoders retain their original behavior.
    scoped = FunctionType(
        original.__code__,
        {**original.__globals__, 'datetime': TransitionClock},
        original.__name__,
        original.__defaults__,
        original.__closure__,
    )
    scoped.__kwdefaults__ = original.__kwdefaults__

    @wraps(original)
    def transition(self, **updates):
        token = floor.set(self.updated_at)
        try:
            return scoped(self, **updates)
        finally:
            floor.reset(token)

    model._transition = transition
    return model
