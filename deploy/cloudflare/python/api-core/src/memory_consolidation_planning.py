"""Fit ordered source batches to the actual unchanged model message budget."""

from dataclasses import dataclass, replace

from memory_consolidation_context import gather_consolidation_context
from memory_consolidation_llm import ConsolidationInferenceError, model_messages
from memory_kernel_consolidation import ConsolidationContext


@dataclass(frozen=True)
class ConsolidationPlan:
    context: ConsolidationContext
    remaining: tuple[str, ...]


def _fits(context):
    try:
        model_messages(context)
    except ConsolidationInferenceError as error:
        if str(error) != 'input_too_large':
            raise
        return False
    return True


def _prefix(context, size):
    pending = context.pending_items[:size]
    return replace(
        context,
        pending_items=pending,
        candidates_by_anchor={item.memory_id: context.candidates_by_anchor.get(item.memory_id, []) for item in pending},
    )


async def plan_consolidation_batch(env, uid, memory_ids):
    """Shrink whole-source prefixes; never trim candidates, feedback or prompts.

    Every reduction regathers context: excluded processed sources become index
    dependencies again. A single over-budget source is returned to the ordinary
    bounded failure/review owner, without taking healthy later sources with it.
    Measuring messages invokes no consolidation LLM or canonical business write.
    """
    ids = tuple(memory_ids)
    context = await gather_consolidation_context(env, uid, ids)
    while len(context.pending_items) > 1 and not _fits(context):
        low, high, size = 1, len(context.pending_items) - 1, 1
        while low <= high:
            middle = (low + high) // 2
            if _fits(_prefix(context, middle)):
                size, low = middle, middle + 1
            else:
                high = middle - 1
        # Candidate/readiness changes during regather can require another
        # strictly smaller prefix; the original 20-item input bounds this loop.
        context = await gather_consolidation_context(env, uid, ids[:size])
    return ConsolidationPlan(context, ids[len(context.pending_items) :])
