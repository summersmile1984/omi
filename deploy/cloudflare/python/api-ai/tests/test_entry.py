import base64
import asyncio
import hashlib
import hmac
import json
import sys
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from entry import _provider_url, transcribe  # noqa: E402


class FakeRequest:
    def __init__(self, env, headers):
        self.scope = {"env": env}
        self.headers = headers

    async def body(self):
        return b"audio"


def signed_context(secret: str) -> tuple[str, str]:
    raw = json.dumps({"uid": "user-1"}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return encoded, base64.urlsafe_b64encode(signature).decode().rstrip("=")


def test_provider_url_normalizes_slashes():
    assert _provider_url("https://asr.example.test/", "/v2/transcribe") == "https://asr.example.test/v2/transcribe"


def test_transcribe_fails_closed_when_provider_is_missing():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
    )

    response = asyncio.run(transcribe(request))

    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "transcription provider is not configured"}
