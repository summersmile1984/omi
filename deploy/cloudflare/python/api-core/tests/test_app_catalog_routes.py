import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from app_catalog_routes import (  # noqa: E402
    get_app_capabilities,
    get_app_categories,
    get_notification_scopes,
    get_payment_plans,
    get_user_payment_plans,
)


def test_static_catalog_metadata_matches_legacy_wire_shapes():
    categories = asyncio.run(get_app_categories())
    scopes = asyncio.run(get_notification_scopes())
    capabilities = asyncio.run(get_app_capabilities())
    payment_plans = asyncio.run(get_payment_plans())

    assert categories[0] == {"title": "Conversation Analysis", "id": "conversation-analysis"}
    assert categories[-1] == {"title": "Other", "id": "other"}
    assert len(categories) == 16
    assert scopes == [
        {"title": "User Name", "id": "user_name"},
        {"title": "User Memories", "id": "user_facts"},
        {"title": "User Conversations", "id": "user_context"},
        {"title": "User Chat", "id": "user_chat"},
    ]
    assert [item["id"] for item in capabilities] == [
        "chat",
        "memories",
        "external_integration",
        "proactive_notification",
    ]
    assert capabilities[2]["triggers"][0] == {"title": "Audio Bytes", "id": "audio_bytes"}
    assert capabilities[2]["actions"][-1]["id"] == "read_tasks"
    assert payment_plans == [{"title": "Monthly Recurring", "id": "monthly_recurring"}]


def test_static_catalog_responses_do_not_share_mutable_state():
    first = asyncio.run(get_app_capabilities())
    first[0]["title"] = "mutated"
    first[2]["triggers"].clear()
    second = asyncio.run(get_app_capabilities())

    assert second[0]["title"] == "Chat"
    assert second[2]["triggers"]


def _plans_request(context=None, reviewers=""):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/app/plans",
            "raw_path": b"/v1/app/plans",
            "query_string": b"",
            "headers": [],
            "scheme": "https",
            "server": ("test", 443),
            "client": ("test", 1),
            "root_path": "",
            "http_version": "1.1",
        }
    )
    request.scope["env"] = SimpleNamespace(MARKETPLACE_APP_REVIEWERS=reviewers)
    if context is not None:
        request.state.auth_context = context
    return request


def test_user_payment_plans_preserve_auth_and_reviewer_boundary():
    unauthorized = asyncio.run(get_user_payment_plans(_plans_request()))
    assert unauthorized.status_code == 401

    reviewer = asyncio.run(
        get_user_payment_plans(
            _plans_request({"uid": "reviewer-1"}, reviewers="reviewer-1,reviewer-2")
        )
    )
    assert reviewer == []

    user = asyncio.run(get_user_payment_plans(_plans_request({"uid": "user-1"})))
    assert user == [{"title": "Monthly Recurring", "id": "monthly_recurring"}]
