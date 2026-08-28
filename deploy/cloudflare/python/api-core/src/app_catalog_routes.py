"""Static application-catalog metadata that is safe to serve from a Worker.

These endpoints intentionally contain no app records or user state. The
mutable catalog, reviews, subscriptions, and MCP credentials remain on their
legacy authority until their database and permission contracts are migrated.
"""

from __future__ import annotations

from copy import deepcopy

from fastapi import APIRouter

router = APIRouter()

_APP_CATEGORIES = [
    {"title": "Conversation Analysis", "id": "conversation-analysis"},
    {"title": "Personality Clone", "id": "personality-emulation"},
    {"title": "Health", "id": "health-and-wellness"},
    {"title": "Education", "id": "education-and-learning"},
    {"title": "Communication", "id": "communication-improvement"},
    {"title": "Emotional Support", "id": "emotional-and-mental-support"},
    {"title": "Productivity", "id": "productivity-and-organization"},
    {"title": "Entertainment", "id": "entertainment-and-fun"},
    {"title": "Financial", "id": "financial"},
    {"title": "Travel", "id": "travel-and-exploration"},
    {"title": "Safety", "id": "safety-and-security"},
    {"title": "Shopping", "id": "shopping-and-commerce"},
    {"title": "Social", "id": "social-and-relationships"},
    {"title": "News", "id": "news-and-information"},
    {"title": "Utilities", "id": "utilities-and-tools"},
    {"title": "Other", "id": "other"},
]

_NOTIFICATION_SCOPES = [
    {"title": "User Name", "id": "user_name"},
    {"title": "User Memories", "id": "user_facts"},
    {"title": "User Conversations", "id": "user_context"},
    {"title": "User Chat", "id": "user_chat"},
]

_APP_CAPABILITIES = [
    {"title": "Chat", "id": "chat"},
    {"title": "Conversations", "id": "memories"},
    {
        "title": "External Integration",
        "id": "external_integration",
        "triggers": [
            {"title": "Audio Bytes", "id": "audio_bytes"},
            {"title": "Conversation Creation", "id": "memory_creation"},
            {"title": "Transcript Processed", "id": "transcript_processed"},
        ],
        "actions": [
            {
                "title": "Create conversations",
                "id": "create_conversation",
                "doc_url": "https://docs.omi.me/doc/developer/apps/Import",
                "description": "Extend user conversations by making a POST request to the OMI System.",
            },
            {
                "title": "Create memories",
                "id": "create_facts",
                "doc_url": "https://docs.omi.me/doc/developer/apps/Import",
                "description": "Create new memories for the user through the OMI System.",
            },
            {
                "title": "Read conversations",
                "id": "read_conversations",
                "doc_url": "https://docs.omi.me/doc/developer/apps/Import",
                "description": "Access and read all user conversations through the OMI System. This gives the app access to all conversation history.",
            },
            {
                "title": "Read memories",
                "id": "read_memories",
                "doc_url": "https://docs.omi.me/doc/developer/apps/Import",
                "description": "Access and read all user memories through the OMI System. This gives the app access to all stored memories.",
            },
            {
                "title": "Read tasks",
                "id": "read_tasks",
                "doc_url": "https://docs.omi.me/doc/developer/apps/Import",
                "description": "Access and read all user tasks (to-dos) through the OMI System. This gives the app access to all stored tasks.",
            },
        ],
    },
    {
        "title": "Notification",
        "id": "proactive_notification",
        "scopes": _NOTIFICATION_SCOPES,
    },
]

_PAYMENT_PLANS = [{"title": "Monthly Recurring", "id": "monthly_recurring"}]


@router.get("/v1/app-categories")
async def get_app_categories() -> list[dict[str, str]]:
    return deepcopy(_APP_CATEGORIES)


@router.get("/v1/app/proactive-notification-scopes")
async def get_notification_scopes() -> list[dict[str, str]]:
    return deepcopy(_NOTIFICATION_SCOPES)


@router.get("/v1/app-capabilities")
async def get_app_capabilities() -> list[dict[str, object]]:
    return deepcopy(_APP_CAPABILITIES)


@router.get("/v1/app/payment-plans")
async def get_payment_plans() -> list[dict[str, str]]:
    return deepcopy(_PAYMENT_PLANS)
