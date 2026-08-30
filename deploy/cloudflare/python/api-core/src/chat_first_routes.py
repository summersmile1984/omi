"""D1-backed Chat-first block capability validation for isolated staging.

The local desktop kernel remains the sole writer of visible Chat turns.  This
route only validates a bounded block union against canonical D1 entities and
returns retry-stable opaque block ids; it never materializes prompts or writes
conversation state.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Union

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, StringConstraints, model_validator

from internal_auth import decode_context

router = APIRouter()

MAX_REQUEST_BYTES = 64_000
STABLE_ID = Annotated[
    str,
    StringConstraints(
        strip_whitespace=False,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChatFirstSubject(_StrictModel):
    kind: Literal["task", "goal", "capture", "cold_start"]
    id: STABLE_ID


class QuestionOption(_StrictModel):
    option_id: STABLE_ID
    label: str = Field(min_length=1, max_length=80)
    prepared_answer: str = Field(min_length=1, max_length=500)
    defer: bool = False


class ColdStartSequence(_StrictModel):
    sequence_id: STABLE_ID
    step: int = Field(ge=1, le=3)


class QuestionCardSpec(_StrictModel):
    type: Literal["questionCard"]
    question_id: STABLE_ID
    text: str = Field(min_length=1, max_length=300)
    subject: ChatFirstSubject
    options: list[QuestionOption] = Field(min_length=1, max_length=4)
    cold_start_sequence: ColdStartSequence | None = None

    @model_validator(mode="after")
    def validate_options(self) -> "QuestionCardSpec":
        option_ids = [option.option_id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("question option IDs must be unique")
        if sum(option.defer for option in self.options) > 1:
            raise ValueError("question card may contain at most one defer option")
        is_cold_start = self.subject.kind == "cold_start"
        if is_cold_start != (self.cold_start_sequence is not None):
            raise ValueError("cold-start subject and sequence descriptor must be paired")
        if self.cold_start_sequence is not None:
            if self.subject.id != self.cold_start_sequence.sequence_id:
                raise ValueError("cold-start subject must match sequence identity")
            if self.cold_start_sequence.step != 1:
                raise ValueError("server cold-start intent must begin at sequence step one")
        return self


class TaskCardSpec(_StrictModel):
    type: Literal["taskCard"]
    task_id: STABLE_ID


class GoalLinkSpec(_StrictModel):
    type: Literal["goalLink"]
    goal_id: STABLE_ID
    summary: str = Field(min_length=1, max_length=200)


class CaptureLinkSpec(_StrictModel):
    type: Literal["captureLink"]
    conversation_id: STABLE_ID
    moment_timestamp_ms: int | None = Field(default=None, ge=0)
    summary: str = Field(min_length=1, max_length=200)


class ConversationLinkActionItemSpec(_StrictModel):
    description: str = Field(min_length=1, max_length=300)
    task_id: STABLE_ID | None = None


class ConversationLinkSpec(_StrictModel):
    type: Literal["conversationLink"]
    conversation_id: STABLE_ID
    summary: str = Field(min_length=1, max_length=200)
    recommended_action_items: list[ConversationLinkActionItemSpec] = Field(default_factory=list, max_length=8)


class MemoryLinkSpec(_StrictModel):
    type: Literal["memoryLink"]
    memory_id: STABLE_ID
    summary: str = Field(min_length=1, max_length=200)


ChatFirstBlock = Annotated[
    Union[QuestionCardSpec, TaskCardSpec, GoalLinkSpec, CaptureLinkSpec, ConversationLinkSpec, MemoryLinkSpec],
    Field(discriminator="type"),
]


class ChatFirstBlockValidationRequest(_StrictModel):
    source_surface: Literal["main_chat"]
    control_generation: int = Field(ge=0)
    owner_fence: STABLE_ID
    run_id: STABLE_ID
    attempt_id: STABLE_ID
    blocks: list[ChatFirstBlock] = Field(min_length=1, max_length=8)


class ChatFirstBlockValidationReceipt(_StrictModel):
    accepted: bool
    code: Literal["accepted", "capability_unavailable", "generation_mismatch", "entity_unavailable", "invalid_request"]
    blocks: list[dict[str, object]] = Field(default_factory=list)


class ChatFirstDeferralRequest(_StrictModel):
    source_surface: Literal["main_chat"]
    control_generation: int = Field(ge=0)
    owner_fence: STABLE_ID
    continuity_key: STABLE_ID
    subject: ChatFirstSubject
    question: QuestionCardSpec

    @model_validator(mode="after")
    def validate_question_subject(self) -> "ChatFirstDeferralRequest":
        if self.question.subject != self.subject:
            raise ValueError("deferral question subject must match deferred subject")
        if self.subject.kind == "cold_start":
            raise ValueError("cold-start question cards cannot be deferred")
        return self


class ChatFirstDeferralReceipt(_StrictModel):
    deferral_id: STABLE_ID
    due_at: datetime
    state: Literal["pending", "released"]


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


async def _bounded_json(request: Request) -> object:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request body is too large")
    return json.loads(raw)


async def _cutover_generation(request: Request, uid: str) -> int | None:
    env = request.scope["env"]
    if getattr(env, "ACCOUNT_CUTOVER_PROFILE", None) != "isolated-staging":
        return None
    row = (
        await env.APP_DB.prepare(
            "SELECT state, account_generation, checkpoint_phase, manifest_id, destination_backend_bound "
            "FROM cf_account_cutover WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
    if not isinstance(row, dict):
        return None
    if (
        row.get("state") != "new"
        or row.get("checkpoint_phase") != "completed"
        or row.get("manifest_id") != "isolated-staging-v1"
        or int(row.get("destination_backend_bound") or 0) != 1
    ):
        return None
    try:
        generation = int(row.get("account_generation"))
    except (TypeError, ValueError):
        return None
    return generation if generation >= 0 else None


async def _entity_available(request: Request, uid: str, block: object) -> bool:
    env = request.scope["env"]

    async def exists(sql: str, *args: object) -> bool:
        row = await env.APP_DB.prepare(sql).bind(*args).first()
        return isinstance(row, dict)

    if isinstance(block, TaskCardSpec):
        return await exists(
            "SELECT id FROM cf_action_items WHERE uid = ? AND id = ? AND deleted = 0 AND is_locked = 0",
            uid,
            block.task_id,
        )
    if isinstance(block, GoalLinkSpec):
        return await exists("SELECT id FROM cf_goals WHERE uid = ? AND id = ? AND is_active = 1", uid, block.goal_id)
    if isinstance(block, MemoryLinkSpec):
        return await exists(
            "SELECT id FROM cf_memories WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL "
            "AND is_locked = 0",
            uid,
            block.memory_id,
        )

    subject = block.subject if isinstance(block, QuestionCardSpec) else None
    if subject is not None:
        if subject.kind == "cold_start":
            return False
        if subject.kind == "task":
            return await exists(
                "SELECT id FROM cf_action_items WHERE uid = ? AND id = ? AND deleted = 0 AND is_locked = 0",
                uid,
                subject.id,
            )
        if subject.kind == "goal":
            return await exists("SELECT id FROM cf_goals WHERE uid = ? AND id = ? AND is_active = 1", uid, subject.id)
        return await exists(
            "SELECT id FROM cf_conversations WHERE uid = ? AND id = ? AND source = 'omi' "
            "AND discarded = 0 AND is_locked = 0",
            uid,
            subject.id,
        )

    if isinstance(block, CaptureLinkSpec):
        return await exists(
            "SELECT id FROM cf_conversations WHERE uid = ? AND id = ? AND source = 'omi' "
            "AND discarded = 0 AND is_locked = 0",
            uid,
            block.conversation_id,
        )
    if isinstance(block, ConversationLinkSpec):
        row = (
            await env.APP_DB.prepare(
                "SELECT status, discarded, is_locked, source, external_data_json FROM cf_conversations "
                "WHERE uid = ? AND id = ?"
            )
            .bind(uid, block.conversation_id)
            .first()
        )
        if not isinstance(row, dict) or row.get("source") != "desktop":
            return False
        if row.get("status") != "completed" or bool(row.get("discarded")) or bool(row.get("is_locked")):
            return False
        raw_external = row.get("external_data_json")
        if not isinstance(raw_external, str):
            return False
        try:
            external = json.loads(raw_external)
        except (TypeError, ValueError):
            return False
        return isinstance(external, dict) and external.get("conversation_role") == "meeting"
    return False


def _stable_block_id(uid: str, generation: int, block: object) -> str:
    canonical = block.model_dump_json(exclude_none=True)
    digest = hashlib.sha256(f"{uid}:{generation}:{canonical}".encode()).hexdigest()[:24]
    return f"cfb_{digest}"


def _stable_deferral_id(uid: str, generation: int, continuity_key: str) -> str:
    raw = "\x1f".join((uid, str(generation), continuity_key)).encode("utf-8")
    return f"cfd_{hashlib.sha256(raw).hexdigest()[:32]}"


def _utc_datetime(epoch_seconds: int) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


@router.post("/v1/chat-first/blocks/validate")
async def validate_chat_first_blocks(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    try:
        payload = ChatFirstBlockValidationRequest.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return ChatFirstBlockValidationReceipt(accepted=False, code="invalid_request")
    if payload.owner_fence != uid:
        return ChatFirstBlockValidationReceipt(accepted=False, code="capability_unavailable")
    try:
        generation = await _cutover_generation(request, uid)
        if generation is None:
            return ChatFirstBlockValidationReceipt(accepted=False, code="capability_unavailable")
        if generation != payload.control_generation:
            return ChatFirstBlockValidationReceipt(accepted=False, code="generation_mismatch")
        for block in payload.blocks:
            if not await _entity_available(request, uid, block):
                return ChatFirstBlockValidationReceipt(accepted=False, code="entity_unavailable")
        block_ids = [_stable_block_id(uid, generation, block) for block in payload.blocks]
        if len(block_ids) != len(set(block_ids)):
            return ChatFirstBlockValidationReceipt(accepted=False, code="invalid_request")
    except Exception:
        return JSONResponse({"error": "chat-first validation unavailable"}, status_code=503)
    return ChatFirstBlockValidationReceipt(
        accepted=True,
        code="accepted",
        blocks=[
            {"id": block_id, **block.model_dump(exclude_none=True)}
            for block_id, block in zip(block_ids, payload.blocks)
        ],
    )


@router.post("/v1/chat/deferrals")
async def record_chat_deferral(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = ChatFirstDeferralRequest.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid deferral"}, status_code=400)
    uid = str(context["uid"])
    if payload.owner_fence != uid:
        return JSONResponse({"error": "deferral capability unavailable"}, status_code=409)
    try:
        generation = await _cutover_generation(request, uid)
        if generation is None:
            return JSONResponse({"error": "deferral capability unavailable"}, status_code=409)
        if generation != payload.control_generation:
            return JSONResponse({"error": "account generation mismatch"}, status_code=409)
        now_seconds = int(time.time())
        due_seconds = now_seconds + int(timedelta(hours=24).total_seconds())
        deferral_id = _stable_deferral_id(uid, generation, payload.continuity_key)
        await request.scope["env"].APP_DB.prepare(
            "INSERT INTO cf_chat_first_deferrals "
            "(uid, deferral_id, continuity_key, account_generation, subject_kind, subject_id, question_json, "
            "created_at, due_at, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending') "
            "ON CONFLICT(uid, deferral_id) DO NOTHING"
        ).bind(
            uid,
            deferral_id,
            payload.continuity_key,
            generation,
            payload.subject.kind,
            payload.subject.id,
            payload.question.model_dump_json(exclude_none=True),
            now_seconds,
            due_seconds,
        ).run()
        existing = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT deferral_id, continuity_key, account_generation, subject_kind, subject_id, question_json, "
                "due_at, state FROM cf_chat_first_deferrals WHERE uid = ? AND deferral_id = ?"
            )
            .bind(uid, deferral_id)
            .first()
        )
        if not isinstance(existing, dict):
            return JSONResponse({"error": "deferral unavailable"}, status_code=503)
        try:
            existing_question = QuestionCardSpec.model_validate(json.loads(str(existing.get("question_json"))))
        except (TypeError, ValueError, ValidationError, json.JSONDecodeError):
            return JSONResponse({"error": "deferral continuity conflict"}, status_code=409)
        if (
            int(existing.get("account_generation") or -1) != generation
            or existing.get("continuity_key") != payload.continuity_key
            or existing.get("subject_kind") != payload.subject.kind
            or existing.get("subject_id") != payload.subject.id
            or existing_question != payload.question
        ):
            return JSONResponse({"error": "deferral continuity conflict"}, status_code=409)
        state = existing.get("state")
        if state not in {"pending", "released"}:
            return JSONResponse({"error": "deferral continuity conflict"}, status_code=409)
        return ChatFirstDeferralReceipt(
            deferral_id=deferral_id,
            due_at=_utc_datetime(int(existing.get("due_at"))),
            state=state,
        )
    except Exception:
        return JSONResponse({"error": "deferral unavailable"}, status_code=503)


__all__ = ["router"]
