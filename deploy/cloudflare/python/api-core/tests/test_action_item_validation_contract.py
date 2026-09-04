"""Actual ASGI input failures compared to the current upstream task contract.

The reference is FastAPI + backend/models/action_item.py, not a duplicated
expected-value schema. Both routes consume the same synthetic HTTP body.
"""

import asyncio
from pathlib import Path
import sys

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parents[5] / "backend"))
from models.action_item import ActionItemCreateRequest, ActionItemUpdateRequest
from action_item_routes import router
from test_action_item_routes import FakeDb, FakeQueue, signed_headers


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/action-items", {}),
        ("POST", "/v1/action-items", {"description": ""}),
        ("POST", "/v1/action-items", {"description": "fixture", "owner": "invalid"}),
        ("POST", "/v1/action-items", {"description": "fixture", "status": "active", "completed": True}),
        ("PATCH", "/v1/action-items/not-created", {"description": ""}),
        ("PATCH", "/v1/action-items/not-created", {"indent_level": 5}),
    ],
)
def test_typed_task_rejection_matches_upstream_fastapi(method, path, body):
    reference = FastAPI()

    @reference.post("/v1/action-items")
    async def create(body: ActionItemCreateRequest):
        return body

    @reference.patch("/v1/action-items/{item_id}")
    async def update(item_id: str, body: ActionItemUpdateRequest):
        return body

    database = FakeDb()
    env = type("Env", (), {"APP_DB": database, "JOBS": FakeQueue(), "INTERNAL_ASSERTION_SECRET": "fixture"})()
    candidate = FastAPI()
    candidate.include_router(router)

    @candidate.middleware("http")
    async def runtime(request, call_next):
        request.scope["env"] = env
        return await call_next(request)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=reference), base_url="http://reference"
        ) as source:
            expected = await source.request(method, path, json=body)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=candidate), base_url="http://candidate"
        ) as target:
            actual = await target.request(method, path, json=body, headers=signed_headers("fixture"))
        assert expected.status_code == actual.status_code == 422
        assert actual.json() == expected.json()
        assert database.connection.execute("SELECT COUNT(*) FROM cf_action_items").fetchone()[0] == 0

    asyncio.run(run())
