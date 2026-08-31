import asyncio
import base64
import json
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import gemini_proxy_routes as gemini  # noqa: E402
import vertex_auth  # noqa: E402

MIGRATION = Path(__file__).parents[3] / "migrations" / "app" / "0114_gemini_proxy.sql"
VERTEX_MIGRATION = Path(__file__).parents[3] / "migrations" / "app" / "0117_gemini_vertex_provider.sql"
VERTEX_PRIVATE_KEY = base64.b64encode(b"\x01" * 256).decode()
VERTEX_SERVICE_ACCOUNT = json.dumps(
    {
        "project_id": "project-123",
        "client_email": "vertex-worker@project-123.iam.gserviceaccount.com",
        "private_key": f"-----BEGIN PRIVATE KEY-----\n{VERTEX_PRIVATE_KEY}\n-----END PRIVATE KEY-----",
        "private_key_id": "vertex-key-1",
    }
)


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


class FakeVertexD1(FakeD1):
    def __init__(self):
        super().__init__()
        self.connection.executescript(VERTEX_MIGRATION.read_text())


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


def test_server_paid_requests_are_capped_to_one_2048_token_candidate(monkeypatch):
    database = FakeD1()
    calls = []

    async def fake_fetch(_url, **options):
        calls.append(options)
        return FakeProviderResponse(b"{}")

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(database),
        json.dumps(
            {
                "contents": [{"parts": [{"text": "hi"}]}],
                "generationConfig": {"candidateCount": 1, "maxOutputTokens": 9000},
            }
        ).encode(),
        path="/v1/proxy/gemini/models/gemini-2.5-flash:generateContent",
        context=context(),
    )

    result = asyncio.run(gemini.gemini_proxy(request, "models/gemini-2.5-flash:generateContent"))

    assert result.status_code == 200
    sent = json.loads(calls[0]["body"])
    assert sent["generationConfig"] == {"candidateCount": 1, "maxOutputTokens": 2048}


def test_server_paid_multi_candidate_request_is_rejected_before_provider(monkeypatch):
    database = FakeD1()
    calls = []

    async def fake_fetch(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("provider must not be called for multi-candidate requests")

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(database),
        b'{"contents":[{"parts":[{"text":"hi"}]}],"generationConfig":{"candidateCount":2}}',
        path="/v1/proxy/gemini/models/gemini-2.5-flash:generateContent",
        context=context(),
    )

    result = asyncio.run(gemini.gemini_proxy(request, "models/gemini-2.5-flash:generateContent"))

    assert result.status_code == 400
    assert json.loads(result.body)["error"] == "invalid_request"
    assert calls == []
    assert database.request_count("user-1") == 0


def test_byok_requests_keep_the_historical_8192_token_ceiling(monkeypatch):
    database = FakeD1()
    calls = []

    async def fake_fetch(_url, **options):
        calls.append(options)
        return FakeProviderResponse(b"{}")

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(database),
        b'{"contents":[{"parts":[{"text":"hi"}]}],"generationConfig":{"maxOutputTokens":9000}}',
        path="/v1/proxy/gemini/models/gemini-2.5-flash:generateContent",
        headers={"x-byok-gemini": "user-gemini-key", "x-omi-request-id": "gemini-byok-1"},
        context=context(byok=True),
    )

    result = asyncio.run(gemini.gemini_proxy(request, "models/gemini-2.5-flash:generateContent"))

    assert result.status_code == 200
    sent = json.loads(calls[0]["body"])
    assert sent["generationConfig"]["maxOutputTokens"] == 8192
    assert calls[0]["headers"]["x-goog-api-key"] == "user-gemini-key"
    assert database.receipt("gemini-byok-1")["status"] == "success"


def test_vertex_service_account_signing_uses_web_crypto_rs256(monkeypatch):
    calls = {}

    class FakeSubtle:
        async def importKey(self, *args):
            calls["import"] = args
            return "crypto-key"

        async def sign(self, *args):
            calls["sign"] = args
            return b"signature"

    fake_js = types.ModuleType("js")
    fake_js.crypto = types.SimpleNamespace(subtle=FakeSubtle())
    monkeypatch.setitem(sys.modules, "js", fake_js)

    signature = asyncio.run(vertex_auth._sign_rs256(b"unsigned", b"private"))

    assert signature == b"signature"
    assert calls["import"][0] == "pkcs8"
    assert calls["import"][2] == {"name": "RSASSA-PKCS1-v1_5", "hash": "SHA-256"}
    assert calls["sign"][0] == "RSASSA-PKCS1-v1_5"
    assert calls["sign"][2] == b"unsigned"


def test_vertex_access_token_sends_bounded_jwt_bearer_and_caches(monkeypatch):
    vertex_auth.clear_access_token_cache()
    signed = {}

    async def fake_sign(unsigned, _private_key):
        signed["unsigned"] = unsigned
        return b"signature"

    monkeypatch.setattr(vertex_auth, "_sign_rs256", fake_sign)
    calls = []

    async def fake_fetch(url, **options):
        calls.append((url, options))
        return FakeProviderResponse(b'{"access_token":"vertex-access-token","expires_in":3600}')

    first = asyncio.run(
        vertex_auth.access_token(
            VERTEX_SERVICE_ACCOUNT,
            fake_fetch,
            expected_project_id="project-123",
            now=1_700_000_000,
        )
    )
    second = asyncio.run(
        vertex_auth.access_token(
            VERTEX_SERVICE_ACCOUNT,
            fake_fetch,
            expected_project_id="project-123",
            now=1_700_000_100,
        )
    )

    assert first == second == "vertex-access-token"
    assert len(calls) == 1
    assert calls[0][0] == vertex_auth.GOOGLE_TOKEN_URL
    form = parse_qs(calls[0][1]["body"])
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    assertion = form["assertion"][0]
    header_raw, claims_raw, _signature = assertion.split(".")
    header = json.loads(base64.urlsafe_b64decode(header_raw + "=="))
    claims = json.loads(base64.urlsafe_b64decode(claims_raw + "=="))
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "vertex-key-1"}
    assert claims == {
        "iss": "vertex-worker@project-123.iam.gserviceaccount.com",
        "scope": vertex_auth.VERTEX_SCOPE,
        "aud": vertex_auth.GOOGLE_TOKEN_URL,
        "iat": 1_700_000_000,
        "exp": 1_700_003_600,
    }
    assert VERTEX_PRIVATE_KEY not in calls[0][1]["body"]


def test_vertex_generate_content_uses_regional_endpoint_and_bearer_auth(monkeypatch):
    database = FakeVertexD1()
    vertex_auth.clear_access_token_cache()

    async def fake_sign(_unsigned, _private_key):
        return b"signature"

    monkeypatch.setattr(vertex_auth, "_sign_rs256", fake_sign)
    calls = []
    provider_body = json.dumps(
        {
            "candidates": [{"content": {"parts": [{"text": "vertex hello"}]}}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3, "totalTokenCount": 8},
        }
    ).encode()

    async def fake_fetch(url, **options):
        calls.append((url, options))
        if url == vertex_auth.GOOGLE_TOKEN_URL:
            return FakeProviderResponse(b'{"access_token":"vertex-access-token","expires_in":3600}')
        return FakeProviderResponse(provider_body)

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(
            database,
            GEMINI_PROXY_PROVIDER="vertex",
            GEMINI_VERTEX_SERVICE_ACCOUNT_JSON=VERTEX_SERVICE_ACCOUNT,
            GEMINI_VERTEX_PROJECT_ID="project-123",
            GEMINI_VERTEX_LOCATION="us-central1",
        ),
        b'{"contents":[{"parts":[{"text":"hi"}]}]}',
        path="/v1/proxy/gemini/models/gemini-2.5-flash:generateContent",
        headers={"x-omi-request-id": "vertex-generate-1"},
        context=context(),
    )

    result = asyncio.run(gemini.gemini_proxy(request, "models/gemini-2.5-flash:generateContent"))

    assert result.status_code == 200
    assert result.headers["x-omi-provider"] == "vertex_ai"
    assert calls[0][0] == vertex_auth.GOOGLE_TOKEN_URL
    assert calls[1][0] == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/project-123/locations/"
        "us-central1/publishers/google/models/gemini-2.5-flash:generateContent"
    )
    assert calls[1][1]["headers"]["authorization"] == "Bearer vertex-access-token"
    assert "x-goog-api-key" not in calls[1][1]["headers"]
    sent = json.loads(calls[1][1]["body"])
    assert sent["generationConfig"] == {"maxOutputTokens": 2048}
    receipt = database.connection.execute(
        "SELECT provider, status, prompt_tokens, output_tokens FROM cf_gemini_usage_receipts "
        "WHERE request_id = 'vertex-generate-1'"
    ).fetchone()
    assert tuple(receipt) == ("vertex_ai", "success", 5, 3)


def test_vertex_embedding_adapts_predict_wire_shape(monkeypatch):
    database = FakeVertexD1()
    vertex_auth.clear_access_token_cache()

    async def fake_sign(_unsigned, _private_key):
        return b"signature"

    monkeypatch.setattr(vertex_auth, "_sign_rs256", fake_sign)
    calls = []

    async def fake_fetch(url, **options):
        calls.append((url, options))
        if url == vertex_auth.GOOGLE_TOKEN_URL:
            return FakeProviderResponse(b'{"access_token":"vertex-access-token","expires_in":3600}')
        return FakeProviderResponse(b'{"predictions":[{"embeddings":{"values":[0.1,0.2]}}]}')

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(
            database,
            GEMINI_PROXY_PROVIDER="vertex_ai",
            GEMINI_VERTEX_SERVICE_ACCOUNT_JSON=VERTEX_SERVICE_ACCOUNT,
        ),
        b'{"content":{"parts":[{"text":"embed me"}]},"taskType":"RETRIEVAL_QUERY"}',
        path="/v1/proxy/gemini/models/gemini-embedding-001:embedContent",
        headers={"x-omi-request-id": "vertex-embed-1"},
        context=context(),
    )

    result = asyncio.run(gemini.gemini_proxy(request, "models/gemini-embedding-001:embedContent"))

    assert result.status_code == 200
    assert json.loads(result.body) == {"embedding": {"values": [0.1, 0.2]}}
    assert calls[1][0].endswith("models/gemini-embedding-001:predict")
    assert json.loads(calls[1][1]["body"]) == {"instances": [{"content": "embed me", "task_type": "RETRIEVAL_QUERY"}]}


def test_vertex_provider_rejects_batch_embeddings_without_dispatch(monkeypatch):
    database = FakeVertexD1()
    calls = []

    async def fake_fetch(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("unsupported Vertex action must not dispatch")

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(
            database,
            GEMINI_PROXY_PROVIDER="vertex",
            GEMINI_VERTEX_SERVICE_ACCOUNT_JSON=VERTEX_SERVICE_ACCOUNT,
        ),
        b'{"requests":[]}',
        path="/v1/proxy/gemini/models/gemini-embedding-001:batchEmbedContents",
        context=context(),
    )

    result = asyncio.run(gemini.gemini_proxy(request, "models/gemini-embedding-001:batchEmbedContents"))

    assert result.status_code == 503
    assert json.loads(result.body)["error"] == "gemini_vertex_action_unavailable"
    assert calls == []
    assert database.request_count("user-1") == 0


def test_vertex_token_rejection_is_sanitized_and_not_retryable(monkeypatch):
    database = FakeVertexD1()
    vertex_auth.clear_access_token_cache()

    async def fake_sign(_unsigned, _private_key):
        return b"signature"

    monkeypatch.setattr(vertex_auth, "_sign_rs256", fake_sign)
    calls = []

    async def fake_fetch(url, **options):
        calls.append((url, options))
        return FakeProviderResponse(
            b'{"error":"invalid_grant","error_description":"private key should not leak"}',
            status=401,
        )

    monkeypatch.setattr(gemini, "worker_fetch", fake_fetch)
    request = FakeRequest(
        make_env(
            database,
            GEMINI_PROXY_PROVIDER="vertex",
            GEMINI_VERTEX_SERVICE_ACCOUNT_JSON=VERTEX_SERVICE_ACCOUNT,
        ),
        b'{"contents":[{"parts":[{"text":"secret prompt"}]}]}',
        path="/v1/proxy/gemini/models/gemini-2.5-flash:generateContent",
        context=context(),
    )

    result = asyncio.run(gemini.gemini_proxy(request, "models/gemini-2.5-flash:generateContent"))

    assert result.status_code == 503
    body = json.loads(result.body)
    assert body["error"] == "gemini_vertex_auth_rejected"
    assert body["retryable"] is False
    assert "private key should not leak" not in result.body.decode()
    assert len(calls) == 1
    assert database.request_count("user-1") == 0


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
