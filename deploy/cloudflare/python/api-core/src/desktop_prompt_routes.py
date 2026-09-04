"""The upstream desktop prompt read contract, backed by operator-owned D1 rows.

The authority is global configuration, not a user projection. Operators write
``cf_desktop_prompts``; no client mutation endpoint or user data is introduced.
Field defaults and audience bucketing follow ``backend/routers/desktop_prompts.py``.
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from internal_auth import decode_context

router = APIRouter()
ALLOWED_TYPES = {"stars", "nps", "choice", "banner"}


class DesktopPromptSpec(BaseModel):
    id: str
    type: str
    question: str
    options: list[str] = []
    cta_label: str | None = None
    cta_url: str | None = None
    trigger_kind: str = "app_launch"
    trigger_count: int = 0
    max_per_day: int = 1


class DesktopPromptsResponse(BaseModel):
    prompts: list[DesktopPromptSpec]


def prompt_matches_audience(doc: dict, uid: str, channel: str, build: int) -> bool:
    audience = doc.get("audience") or {}
    channels = audience.get("channels") or []
    if channels and channel not in channels:
        return False
    min_build = audience.get("min_build") or 0
    if build and min_build and build < min_build:
        return False
    percentage = audience.get("rollout_pct")
    if percentage is None:
        percentage = 100
    bucket = int(hashlib.sha256(f'{doc.get("id")}:{uid}'.encode()).hexdigest()[:8], 16) % 100
    return bucket < int(percentage)


def spec_from_doc(doc: dict) -> DesktopPromptSpec | None:
    if not doc.get("id") or doc.get("type") not in ALLOWED_TYPES or not doc.get("question"):
        return None
    trigger, cta = doc.get("trigger") or {}, doc.get("cta") or {}
    return DesktopPromptSpec(
        id=str(doc["id"]),
        type=doc["type"],
        question=str(doc["question"]),
        options=[str(option) for option in (doc.get("options") or [])][:6],
        cta_label=cta.get("label"),
        cta_url=cta.get("url"),
        trigger_kind=str(trigger.get("kind") or "app_launch"),
        trigger_count=int(trigger.get("count") or 0),
        max_per_day=int(doc.get("max_per_day") or 1),
    )


@router.get("/v2/desktop/prompts", response_model=DesktopPromptsResponse)
async def get_desktop_prompts(request: Request, channel: str = "stable", build: int = 0):
    env = request.scope["env"]
    context = decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = await env.APP_DB.prepare(
            "SELECT id, document_json FROM cf_desktop_prompts WHERE active = 1 ORDER BY id LIMIT 50"
        ).all()
        prompts = []
        for row in result.get("results", []):
            doc = json.loads(row["document_json"])
            doc.setdefault("id", row["id"])
            if prompt_matches_audience(doc, str(context["uid"]), channel, build):
                spec = spec_from_doc(doc)
                if spec is not None:
                    prompts.append(spec)
    except Exception:
        return JSONResponse({"detail": "Desktop prompts unavailable"}, status_code=503)
    return DesktopPromptsResponse(prompts=sorted(prompts, key=lambda prompt: prompt.id))
