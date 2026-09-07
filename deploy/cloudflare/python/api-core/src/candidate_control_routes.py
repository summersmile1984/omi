"""Original universal task capability bound to the authoritative D1 generation."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context
from candidate_db import account_generation
from candidate_kernel_policy import CandidateGenerationMismatchError
from memory_kernel_task_intelligence import TaskWorkflowControl
from candidate_kernel_rollout import effective_task_workflow_control, resolve_task_intelligence_for_user
from fallback import record_fallback

router = APIRouter()


def _context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


@router.get("/v1/candidates/control")
async def get_candidate_workflow_control(request: Request):
    """Use the same account-generation owner as canonical reads and writers.

    The original rollout is universal: no persisted workflow/UI flag grants
    capability. An unmigrated principal has generation zero; an unavailable
    or fenced account retains the original fail-closed response.
    """
    principal = _context(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        generation = await account_generation(request.scope['env'], principal['uid'])
        control = TaskWorkflowControl(account_generation=generation)
        rollout = resolve_task_intelligence_for_user(
            uid=principal['uid'], workflow_mode=control.workflow_mode, account_generation=generation
        )
        return effective_task_workflow_control(control, rollout).model_dump(mode='json')
    except Exception as error:
        record_fallback(
            from_mode='task_workflow_control',
            to_mode='legacy_shell',
            reason='auth' if isinstance(error, CandidateGenerationMismatchError) else 'dependency_unavailable',
            outcome='degraded',
        )
        return TaskWorkflowControl().model_dump(mode='json')
