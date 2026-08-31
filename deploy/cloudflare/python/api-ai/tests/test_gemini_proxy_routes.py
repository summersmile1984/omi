import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import gemini_proxy_routes as gemini  # noqa: E402

MIGRATION = Path(__file__).parents[3] / "migrations" / "app" / "0114_gemini_proxy.sql"


class FakeStatement:
    def __init__(self, database, sql):
        self.database = database
        self.sql = sql
        self.values = ()

    def bind(self, *values):
        self.values = values
        return self

    async def run(self):
        if self.database.fail:
            raise RuntimeError("simulated D1 failure")
        self.database.connection.execute(self.sql, self.values)
        self.database.connection.commit()
        return {"success": True}

    async def first(self):
        if self.database.fail:
            raise RuntimeError("simulated D1 failure")
        row = self.database.connection.execute(self.sql, self.values).fetchone()
        return dict(row) if row is not None else None


class FakeD1:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            "CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);"
            "CREATE TABLE cf_account_deletion_tombstones (uid TEXT PRIMARY KEY);"
        )
        self.connection.executescript(MIGRATION.read_text())

    def prepare(self, sql):
        return FakeStatement(self, sql)

    async def batch(self, statements):
        if self.fail:
            raise RuntimeError("simulated D1 failure")
        try:
            self.connection.execute("BEGIN")
            for statement in statements:
                self.connection.execute(statement.sql, statement.values)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def receipt(self, request_id):
        row = self.connection.execute(
            "SELECT uid, status, prompt_tokens, output_tokens, estimated_cost_micros "
            "FROM cf_gemini_usage_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return dict(row) if row else None

    def request_count(self, uid):
        row = self.connection.execute(
            "SELECT request_count FROM cf_gemini_quota_windows WHERE uid = ?", (uid,)
        ).fetchone()
        return row[0] if row else 0


class FakeRequest:
    def __init__(self, env, body, *, path, headers=None, context=None):
        self.scope = {"env": env}
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.state = SimpleNamespace(auth_context=context)
        parsed = urlsplit(f"https://api.test{path}")
        self.url = SimpleNamespace(query=parsed.query)
        self._body = body

    async def body(self):
        return self._body


class FakeProviderResponse:
    def __init__(self, body, *, status=200, content_type="application/json"):
        self.status = status
        self.headers = {"content-type": content_type}
        self._body = body
        self.body = None

    async def arrayBuffer(self):
        return self._body


class FakeSseReader:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    async def read(self):
        try:
            return {"done": False, "value": next(self.chunks)}
        except StopIteration:
            return {"done": True, "value": None}


class FakeStreamingProviderResponse(FakeProviderResponse):
    def __init__(self, chunks):
        super().__init__(b"", content_type="text/event-stream")
        self.body = SimpleNamespace(getReader=lambda: FakeSseReader(chunks))


def make_env(database, **overrides):
    values = {
        "APP_DB": database,
        "GEMINI_PROXY_ENABLED": "true",
        "GEMINI_PROXY_PROVIDER": "ai_studio",
        "GEMINI_API_KEY": "server-key",
        "GEMINI_DAILY_LIMIT": "1500",
        "GEMINI_INPUT_USD_PER_MILLION": "0.1",
        "GEMINI_OUTPUT_USD_PER_MILLION": "0.4",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def context(*, byok=False):
    return {"uid": "user-1", "accountGeneration": 2, "byokActive": byok}


def test_generate_content_uses_direct_gemini_rest_and_records_usage(monkeypatch):
    database = FakeD1()
    response_body = json.dumps(
        {
            "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 4,
                "totalTokenCount": 14,
                "trafficType": "ON_DEMAND",
            },
        }
    ).encode()
    calls = {}

    async def fake_fetch(url, **options):
        calls["url"] = url
        calls["options"] = options
        return FakeProviderResponse(response_body)

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(database),
        json.dumps(
            {
                "contents": [
                    {"role": "system", "parts": [{"text": "be concise"}]},
                    {"role": "user", "parts": [{"text": "hi"}]},
                ],
                "safetySettings": [{"category": "caller-controlled"}],
            }
        ).encode(),
        path="/v1/proxy/gemini/models/gemini-2.5-flash:generateContent",
        headers={"x-omi-request-id": "gemini-test-1"},
        context=context(),
    )

    result = asyncio.run(gemini.gemini_proxy(request, "models/gemini-2.5-flash:generateContent"))

    assert result.status_code == 200
    assert json.loads(result.body) == json.loads(response_body)
    assert calls["url"] == ("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent")
    assert calls["options"]["headers"]["x-goog-api-key"] == "server-key"
    sent = json.loads(calls["options"]["body"])
    assert "safetySettings" not in sent
    assert sent["systemInstruction"] == {"parts": [{"text": "be concise"}]}
    assert database.receipt("gemini-test-1") == {
        "uid": "user-1",
        "status": "success",
        "prompt_tokens": 10,
        "output_tokens": 4,
        "estimated_cost_micros": 3,
    }
    assert database.request_count("user-1") == 1


def test_provider_query_is_forwarded_without_reconstructing_each_character(monkeypatch):
    database = FakeD1()
    calls = []

    async def fake_fetch(url, **_options):
        calls.append(url)
        return FakeProviderResponse(b"{}")

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(database),
        b'{"contents":[{"parts":[{"text":"hi"}]}]}',
        path="/v1/proxy/gemini/models/gemini-2.5-flash:generateContent?foo=bar&x=1",
        context=context(),
    )
    request.url.query = "foo=bar&x=1"

    result = asyncio.run(gemini.gemini_proxy(request, "models/gemini-2.5-flash:generateContent"))

    assert result.status_code == 200
    assert calls == [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?foo=bar&x=1"
    ]


def test_provider_secret_is_fail_closed_without_calling_gemini(monkeypatch):
    database = FakeD1()
    calls = []

    async def fake_fetch(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("provider must not be called without a key")

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(database, GEMINI_API_KEY=""),
        b'{"contents":[{"parts":[{"text":"hi"}]}]}',
        path="/v1/proxy/gemini/models/gemini-2.5-flash:generateContent",
        context=context(),
    )

    result = asyncio.run(gemini.gemini_proxy(request, "models/gemini-2.5-flash:generateContent"))

    assert result.status_code == 503
    assert json.loads(result.body)["error"] == "gemini_provider_unavailable"
    assert calls == []
    assert database.request_count("user-1") == 0


def test_stream_generate_content_preserves_gemini_sse_and_settles_usage(monkeypatch):
    database = FakeD1()
    chunks = [
        b'data: {"candidates":[{"content":{"parts":[{"text":"hel"}]}}]}\r\n\r\n',
        b'data: {"candidates":[{"content":{"parts":[{"text":"lo"}]}}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":3,"totalTokenCount":5}}\n\n',
    ]
    calls = []

    async def fake_fetch(url, **options):
        calls.append((url, options))
        return FakeStreamingProviderResponse(chunks)

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(database),
        b'{"contents":[{"parts":[{"text":"hi"}]}]}',
        path="/v1/proxy/gemini-stream/models/gemini-2.5-flash:streamGenerateContent",
        headers={"x-omi-request-id": "gemini-stream-1"},
        context=context(),
    )

    result = asyncio.run(gemini.gemini_stream_proxy(request, "models/gemini-2.5-flash:streamGenerateContent"))
    body = b"".join(asyncio.run(collect_async(result.body_iterator)))

    assert result.status_code == 200
    assert body == b"".join(chunks)
    assert calls[0][0].endswith("models/gemini-2.5-flash:streamGenerateContent?alt=sse")
    assert database.receipt("gemini-stream-1")["status"] == "success"


async def collect_async(iterator):
    return [chunk async for chunk in iterator]


def test_daily_limit_is_reserved_once_and_rejects_new_request(monkeypatch):
    database = FakeD1()
    calls = []

    async def fake_fetch(url, **options):
        calls.append((url, options))
        return FakeProviderResponse(b'{"usageMetadata":{"totalTokenCount":1}}')

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    env = make_env(database, GEMINI_DAILY_LIMIT="1")

    def run(request_id):
        request = FakeRequest(
            env,
            b'{"contents":[{"parts":[{"text":"hi"}]}]}',
            path="/v1/proxy/gemini/models/gemini-2.5-flash:generateContent",
            headers={"x-omi-request-id": request_id},
            context=context(),
        )
        return asyncio.run(gemini.gemini_proxy(request, "models/gemini-2.5-flash:generateContent"))

    assert run("gemini-daily-1").status_code == 200
    second = run("gemini-daily-2")
    assert second.status_code == 429
    assert json.loads(second.body)["error"] == "gemini_daily_quota_exceeded"
    assert len(calls) == 1
    assert database.request_count("user-1") == 1


def test_deletion_fence_rejects_admission_before_provider_call(monkeypatch):
    database = FakeD1()
    database.connection.execute("INSERT INTO cf_account_deletion_intents (uid) VALUES ('user-1')")
    database.connection.commit()
    calls = []

    async def fake_fetch(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("provider must not be called after deletion fence")

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(database),
        b'{"contents":[{"parts":[{"text":"hi"}]}]}',
        path="/v1/proxy/gemini/models/gemini-2.5-flash:generateContent",
        context=context(),
    )

    result = asyncio.run(gemini.gemini_proxy(request, "models/gemini-2.5-flash:generateContent"))

    assert result.status_code == 503
    assert json.loads(result.body)["error"] == "gemini_usage_unavailable"
    assert calls == []
    assert database.request_count("user-1") == 0


def test_stream_route_requires_stream_generate_action(monkeypatch):
    database = FakeD1()
    monkeypatch.setattr(gemini, "worker_fetch", lambda *_args, **_kwargs: None)
    request = FakeRequest(
        make_env(database),
        b'{"contents":[{"parts":[{"text":"hi"}]}]}',
        path="/v1/proxy/gemini-stream/models/gemini-2.5-flash:generateContent",
        context=context(),
    )

    result = asyncio.run(gemini.gemini_stream_proxy(request, "models/gemini-2.5-flash:generateContent"))

    assert result.status_code == 400
    assert json.loads(result.body)["error"] == "invalid_request"


@pytest.mark.parametrize("path", ["models/gemini-2.5-flash:generateContent?key=leak", "models/gemini-2.5-pro:unknown"])
def test_provider_credential_query_and_unknown_action_are_rejected(path):
    database = FakeD1()
    request_path, _, query = path.partition("?")
    request = FakeRequest(
        make_env(database),
        b'{"contents":[{"parts":[{"text":"hi"}]}]}',
        path=f"/v1/proxy/gemini/{path}",
        context=context(),
    )
    request.url.query = query

    result = asyncio.run(gemini.gemini_proxy(request, request_path))

    assert result.status_code == (400 if query else 403)
    assert database.request_count("user-1") == 0
