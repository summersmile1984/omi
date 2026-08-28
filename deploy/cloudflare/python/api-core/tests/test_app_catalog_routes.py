import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from app_catalog_routes import (  # noqa: E402
    get_app_capabilities,
    get_app_categories,
    get_notification_scopes,
    get_payment_plans,
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
