"""Retain hydrated candidate identities across the upstream planner boundary."""

from dataclasses import dataclass, field, fields, replace
from functools import wraps
from types import FunctionType, MappingProxyType
from typing import Mapping

from .consolidation_admission import MemoryIdentity, validate_duplicate_creates


def gather(original):
    context_type = original.__globals__['ConsolidationContext']
    hydrate = original.__globals__['_hydrate_memory_item']

    @dataclass
    class AdmissionContext(context_type):
        admission_memories: Mapping[str, MemoryIdentity] = field(default_factory=dict)

    @wraps(original)
    def wrapped(uid, pending_items, *, db_client=None, candidate_limit=None):
        identities = {item.memory_id: MemoryIdentity.from_item(item) for item in pending_items}

        def hydrate_identity(*args, **kwargs):
            item = hydrate(*args, **kwargs)
            if item is not None:
                identities[item.memory_id] = MemoryIdentity.from_item(item)
            return item

        # Bind the original gather code to this invocation's hydration seam.
        # No module-global mutation, additional database reads, or prompt fields.
        invoke = FunctionType(
            original.__code__,
            {**original.__globals__, '_hydrate_memory_item': hydrate_identity},
            original.__name__,
            original.__defaults__,
            original.__closure__,
        )
        invoke.__kwdefaults__ = original.__kwdefaults__
        context = invoke(uid, pending_items, db_client=db_client, candidate_limit=candidate_limit)
        return AdmissionContext(
            **{entry.name: getattr(context, entry.name) for entry in fields(context)},
            admission_memories=MappingProxyType(identities),
        )

    return wrapped


def validate(original):
    @wraps(original)
    def wrapped(context, batch):
        identities = getattr(context, 'admission_memories', None)
        pending = None
        for index, item in enumerate(context.pending_items):
            identity = identities.get(item.memory_id) if identities is not None else None
            if item.subject_entity_id is None and identity is not None and identity.subject:
                if pending is None:
                    pending = list(context.pending_items)
                pending[index] = item.model_copy(update={'subject_entity_id': identity.subject})
        # Validate against the authoritative source identity when its derived
        # graph subject was cleared. Never change the source snapshot, bypass
        # the remaining attribution checks, or synthesize a routing decision.
        validation_context = replace(context, pending_items=pending) if pending is not None else context
        error = original(validation_context, batch)
        if error is not None:
            return error
        if identities is None:
            return 'output_invalid:hydrated_identity_context_missing'
        return validate_duplicate_creates(context, batch, identities)

    return wrapped
