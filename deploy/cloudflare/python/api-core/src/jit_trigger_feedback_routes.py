"""Explicit user feedback stays available when proactive rollout is off or killed."""

import json
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from candidate_routes import context
from jit_trigger_feedback_kernel import apply_canonical_trigger_feedback, TriggerFeedback
from jit_trigger_feedback_store import FeedbackStore
from jit_trigger_feedback_wire import JITTriggerFeedbackRequest, JITTriggerFeedbackEnvelope
from fallback import record_fallback

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post('/v1/jit/trigger-feedback')
async def trigger_feedback(request: Request):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 8192:
                raise ValueError('request too large')
            raw.extend(chunk)
        value = JITTriggerFeedbackRequest.model_validate(json.loads(raw))
        feedback = TriggerFeedback(
            feedback_id=value.feedback_id,
            action=value.action,
            recorded_at=value.recorded_at,
            snoozed_until=value.snoozed_until,
        )
    except (ValueError, TypeError):
        return JSONResponse({'detail': 'Invalid JIT trigger feedback'}, status_code=422)
    for attempt in range(3):
        try:
            result = await apply_canonical_trigger_feedback(
                str(principal['uid']),
                value.trigger_memory_id,
                event_id=value.event_id,
                expected_account_generation=value.account_generation,
                expected_item_revision=value.trigger_revision,
                feedback=feedback,
                store=FeedbackStore(request.scope['env'], str(principal['uid'])),
            )
            return JSONResponse(
                JITTriggerFeedbackEnvelope(
                    applied=result.applied,
                    trigger_memory_id=result.item.memory_id,
                    trigger_revision=result.item.item_revision,
                    trigger_status=result.item.status.value,
                    receipt=result.receipt,
                ).model_dump(mode='json')
            )
        except Exception as error:
            if attempt < 2 and any(
                reason in str(error)
                for reason in (
                    'candidate_snapshot_changed',
                    'memory_apply_head_changed',
                    'memory_apply_target_changed',
                    'memory_apply_observation_changed',
                    'memory_apply_operation_changed',
                )
            ):
                continue
            # Log code locations and type only: database errors may contain SQL
            # values and model errors may contain the user's memory arguments.
            trace = error.__traceback__
            locations = []
            while trace is not None:
                code = trace.tb_frame.f_code
                locations.append([code.co_filename.rsplit('/', 1)[-1], code.co_name, trace.tb_lineno])
                trace = trace.tb_next
            logger.warning(
                'jit_trigger_feedback_rejected %s',
                json.dumps(
                    {
                        'error_class': type(error).__name__,
                        'locations': locations[-4:],
                        'storage_reason': next(
                            (
                                reason
                                for reason in (
                                    'trigger_feedback_canonical_apply_required',
                                    'candidate_apply_required',
                                    'memory_apply_account_deleted',
                                    'memory_apply_generation_changed',
                                    'memory_apply_target_changed',
                                    'memory_apply_observation_changed',
                                    'memory_apply_head_changed',
                                    'candidate_snapshot_changed',
                                    'too many SQL variables',
                                    'LIKE or GLOB pattern too complex',
                                    'too many levels of trigger recursion',
                                    'NOT NULL constraint failed',
                                    'CHECK constraint failed',
                                    'UNIQUE constraint failed',
                                    'FOREIGN KEY constraint failed',
                                    'no such column',
                                    'syntax error',
                                    'D1_TYPE_ERROR',
                                )
                                if reason in str(error)
                            ),
                            'unclassified',
                        ),
                    }
                ),
            )
            if not isinstance(error, (ValueError, RuntimeError)):
                record_fallback(
                    component='other',
                    from_mode='none',
                    to_mode='none',
                    reason='dependency_unavailable',
                    outcome='exhausted',
                )
            return JSONResponse({'detail': 'Trigger feedback authority changed or is unavailable'}, status_code=409)
