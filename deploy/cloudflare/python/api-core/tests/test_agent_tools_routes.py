import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_tools_routes import CLOUDFLARE_TOOL_DEFINITIONS, list_tools  # noqa: E402

SECRET = "agent-tools-test-secret"


class FakeRequest:
    def __init__(self, *, authenticated: bool):
        self.scope = {"env": SimpleNamespace(INTERNAL_ASSERTION_SECRET=SECRET)}
        if authenticated:
            payload = json.dumps(
                {"uid": "agent-tools-user", "authority": "better-auth"}, separators=(",", ":")
            ).encode()
            encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
            signature = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
            self.headers = {
                "x-omi-auth-context": encoded,
                "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
            }
        else:
            self.headers = {}


def run(awaitable):
    return asyncio.run(awaitable)


def test_agent_tool_directory_requires_authentication():
    response = run(list_tools(FakeRequest(authenticated=False)))
    assert response.status_code == 401


def test_agent_tool_directory_exposes_only_cloudflare_native_tools():
    result = run(list_tools(FakeRequest(authenticated=True)))
    names = [tool["name"] for tool in result["tools"]]
    assert names == [tool["name"] for tool in CLOUDFLARE_TOOL_DEFINITIONS]
    assert names == [
        "get_conversations_tool",
        "search_conversations_tool",
        "get_memories_tool",
        "search_memories_tool",
        "get_action_items_tool",
        "create_action_item_tool",
        "update_action_item_tool",
    ]
    assert "config" not in json.dumps(result)
    assert result["tools"][1]["parameters"]["required"] == ["query"]
    assert result["tools"][5]["parameters"]["required"] == ["description"]


def test_agent_tool_directory_returns_a_deep_copy():
    result = run(list_tools(FakeRequest(authenticated=True)))
    result["tools"][0]["parameters"]["properties"]["limit"]["default"] = 999
    assert CLOUDFLARE_TOOL_DEFINITIONS[0]["parameters"]["properties"]["limit"]["default"] == 20
