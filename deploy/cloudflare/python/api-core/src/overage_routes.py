"""D1-backed chat overage projection for the plan explainer UI."""

from __future__ import annotations

import math

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from chat_quota import PLAN_DISPLAY_NAMES, monthly_chat_usage, plan_policy, subscription_plan
from internal_auth import decode_context

router = APIRouter()

DEFAULT_OVERAGE_MARKUP_MULTIPLIER = 1.15

PROVIDER_REFERENCE_RATES = {
    "claude_sonnet_input_per_mtok": 3.00,
    "claude_sonnet_output_per_mtok": 15.00,
    "gemini_flash_input_per_mtok": 0.30,
    "gemini_flash_output_per_mtok": 2.50,
    "gpt_4_1_mini_input_per_mtok": 0.40,
    "gpt_4_1_mini_output_per_mtok": 1.60,
    "deepgram_nova_per_min": 0.0043,
}

OVERAGE_EXPLAINER_TITLE = "What happens past your monthly limit?"

OVERAGE_EXPLAINER_BODY = (
    "Your paid plan includes a monthly AI-usage allowance. If you go over, Omi "
    "doesn't cut you off — you stay fully functional and we charge only for "
    "the extra usage, billed to the card on file at the end of your cycle.\n\n"
    "How the charge is computed:\n"
    "  • We sum the real provider cost (Claude, Gemini, Deepgram, etc.) of the "
    "usage past your included allowance.\n"
    "  • We add a {markup_pct:.0f}% buffer on top to cover infra and pricing variance.\n"
    "  • That's it — no surge pricing, no hidden fees.\n\n"
    "A typical chat question costs roughly $0.01–$0.05 of real compute. Heavy "
    "RAG or agentic questions cost a bit more.\n\n"
    "Prefer predictable billing? Bring your own API keys in Settings → Developer "
    "API Keys and pay providers directly — Omi is free when BYOK is active."
)


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _markup_multiplier(env: object) -> float:
    try:
        value = float(getattr(env, "OVERAGE_MARKUP_MULTIPLIER", DEFAULT_OVERAGE_MARKUP_MULTIPLIER))
    except (TypeError, ValueError):
        return DEFAULT_OVERAGE_MARKUP_MULTIPLIER
    return value if math.isfinite(value) and value > 0 else DEFAULT_OVERAGE_MARKUP_MULTIPLIER


def _included_allowance(env: object, plan: str) -> tuple[int | None, float | None]:
    if plan in {"unlimited", "operator"}:
        return int(float(plan_policy(env, plan)["limit"])), None
    if plan == "architect":
        return None, float(plan_policy(env, plan)["limit"])
    return None, None


@router.get("/v1/payments/overage-info")
async def get_overage_info(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    try:
        plan = await subscription_plan(env, str(context["uid"]))
        usage = await monthly_chat_usage(env, str(context["uid"]))
        if int(usage["unsettled"]) > 0:
            raise RuntimeError("chat overage projection has unsettled provider cost")
    except Exception:
        return JSONResponse({"error": "overage info unavailable"}, status_code=503)

    included_questions, included_cost_usd = _included_allowance(env, plan)
    used_questions = int(usage["questions"])
    real_cost_usd = round(float(usage["cost_usd"]), 4)
    markup_multiplier = _markup_multiplier(env)
    excess_questions = 0
    overage_usd = 0.0
    if included_questions is not None and used_questions > included_questions and used_questions > 0:
        excess_questions = used_questions - included_questions
        overage_usd = round(
            (excess_questions / used_questions) * real_cost_usd * markup_multiplier,
            4,
        )
    elif included_cost_usd is not None and real_cost_usd > included_cost_usd:
        overage_usd = round((real_cost_usd - included_cost_usd) * markup_multiplier, 4)

    markup_percent = round((markup_multiplier - 1.0) * 100.0, 2)
    return {
        "plan": PLAN_DISPLAY_NAMES[plan],
        "plan_type": plan,
        "is_overage_plan": included_questions is not None or included_cost_usd is not None,
        "included_questions": included_questions,
        "included_cost_usd": included_cost_usd,
        "used_questions": used_questions,
        "excess_questions": excess_questions,
        "real_cost_usd": real_cost_usd,
        "overage_usd": overage_usd,
        "markup_multiplier": markup_multiplier,
        "markup_percent": markup_percent,
        "reset_at": usage["reset_at"],
        "explainer_title": OVERAGE_EXPLAINER_TITLE,
        "explainer_body": OVERAGE_EXPLAINER_BODY.format(markup_pct=markup_percent),
        "provider_reference_rates": PROVIDER_REFERENCE_RATES,
        "byok_available": True,
    }


__all__ = [
    "DEFAULT_OVERAGE_MARKUP_MULTIPLIER",
    "OVERAGE_EXPLAINER_BODY",
    "OVERAGE_EXPLAINER_TITLE",
    "PROVIDER_REFERENCE_RATES",
    "get_overage_info",
    "router",
]
