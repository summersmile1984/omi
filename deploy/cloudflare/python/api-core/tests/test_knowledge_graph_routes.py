import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from knowledge_graph_routes import (  # noqa: E402
    delete_knowledge_graph,
    extract_knowledge_graph,
    get_canonical_knowledge_graph,
    get_knowledge_graph,
    rebuild_knowledge_graph,
)


class FakeStatement:
    def __init__(self, connection, sql, args=()):
        self.connection = connection
        self.sql = sql
        self.args = args

    def bind(self, *args):
        return FakeStatement(self.connection, self.sql, args)

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row is not None else None

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for name in ("0034_account_cutover.sql", "0037_memories.sql"):
            self.connection.executescript((migration_dir / name).read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeRequest:
    def __init__(self, env, headers, *, query=None, body=None):
        self.scope = {"env": env}
        self.headers = headers
        self.query_params = query or {}
        self._body = b"" if body is None else json.dumps(body).encode()

    async def body(self):
        return self._body


class FakeAi:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        return self.response


def signed_headers(secret: str, uid: str = "graph-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "graph-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def environment(secret: str = "graph-secret", *, ai=None):
    return type(
        "Env",
        (),
        {
            "APP_DB": FakeDb(),
            "INTERNAL_ASSERTION_SECRET": secret,
            "AI": ai,
            "WORKERS_AI_KNOWLEDGE_GRAPH_MODEL": "test-graph-model",
        },
    )()


def insert_cutover(env, uid="graph-user", state="new"):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_account_cutover (uid, state, updated_at) VALUES (?, ?, 1)",
        (uid, state),
    )
    env.APP_DB.connection.commit()


def insert_memory(
    env,
    memory_id,
    *,
    uid="graph-user",
    updated_at=10,
    content="The user lives in Shanghai.",
    subject="user",
    predicate="lives_in",
    arguments=None,
    object_ids=None,
    tier="long_term",
    locked=0,
    user_review=None,
):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_memories "
        "(uid, id, content, predicate, arguments_json, subject_entity_id, object_entity_ids_json, "
        "memory_tier, is_locked, user_review, valid_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
            memory_id,
            content,
            predicate,
            json.dumps(arguments if arguments is not None else {"place": "Shanghai"}),
            subject,
            json.dumps(object_ids or []),
            tier,
            locked,
            user_review,
            updated_at,
            updated_at,
            updated_at,
        ),
    )
    env.APP_DB.connection.commit()


def response_json(response):
    return json.loads(response.body)


def test_legacy_graph_get_is_authenticated_empty_and_bounded():
    secret = "graph-secret"
    env = environment(secret)
    insert_cutover(env)

    unauthorized = asyncio.run(get_knowledge_graph(FakeRequest(env, {})))
    assert unauthorized.status_code == 401

    graph = asyncio.run(get_knowledge_graph(FakeRequest(env, signed_headers(secret))))
    assert graph == {
        "nodes": [],
        "edges": [],
        "truncated": False,
        "node_count": 0,
        "edge_count": 0,
        "node_limit": 500,
        "edge_limit": 500,
    }


def test_graph_is_derived_only_from_visible_long_term_uid_scoped_d1_memories():
    secret = "graph-secret"
    env = environment(secret)
    insert_cutover(env)
    insert_memory(
        env,
        "visible",
        arguments={"place": {"entity_id": "ent_shanghai", "label": "Shanghai", "node_type": "place"}},
    )
    insert_memory(env, "short", tier="short_term", arguments={"place": "Paris"})
    insert_memory(env, "locked", locked=1, arguments={"place": "Tokyo"})
    insert_memory(env, "rejected", user_review=0, arguments={"place": "London"})
    insert_memory(env, "other-user", uid="stranger", arguments={"place": "Berlin"})

    graph = asyncio.run(get_knowledge_graph(FakeRequest(env, signed_headers(secret))))
    assert graph["node_count"] == 2
    assert {node["id"] for node in graph["nodes"]} == {"user", "ent_shanghai"}
    shanghai = next(node for node in graph["nodes"] if node["id"] == "ent_shanghai")
    assert shanghai == {
        "id": "ent_shanghai",
        "label": "Shanghai",
        "node_type": "place",
        "aliases": [],
        "memory_ids": ["visible"],
    }
    assert graph["edges"][0]["source_id"] == "user"
    assert graph["edges"][0]["target_id"] == "ent_shanghai"
    assert graph["edges"][0]["memory_ids"] == ["visible"]


def test_canonical_graph_pages_with_signed_revision_fenced_cursor():
    secret = "graph-secret"
    env = environment(secret)
    insert_cutover(env)
    insert_memory(env, "older", updated_at=10, content="Older", arguments={"place": "Paris"})
    insert_memory(env, "newer", updated_at=20, content="Newer", arguments={"place": "Shanghai"})

    first = asyncio.run(get_canonical_knowledge_graph(FakeRequest(env, signed_headers(secret), query={"limit": "1"})))
    assert first["has_more"] is True
    assert first["next_cursor"]
    assert first["catalog_nodes"][0]["memory_ids"] == ["newer"]

    second = asyncio.run(
        get_canonical_knowledge_graph(
            FakeRequest(env, signed_headers(secret), query={"limit": "1", "cursor": first["next_cursor"]})
        )
    )
    assert second["has_more"] is False
    assert second["next_cursor"] is None
    assert second["catalog_nodes"][0]["memory_ids"] == ["older"]

    env.APP_DB.connection.execute(
        "UPDATE cf_memories SET updated_at = 30 WHERE uid = ? AND id = ?", ("graph-user", "older")
    )
    env.APP_DB.connection.commit()
    stale = asyncio.run(
        get_canonical_knowledge_graph(
            FakeRequest(env, signed_headers(secret), query={"limit": "1", "cursor": first["next_cursor"]})
        )
    )
    assert stale.status_code == 400
    assert response_json(stale) == {"detail": "invalid_or_stale_cursor"}


def test_delete_and_rebuild_preserve_canonical_state_and_fail_closed_when_unverified():
    secret = "graph-secret"
    env = environment(secret)
    insert_cutover(env)
    insert_memory(env, "visible")

    rebuilt = asyncio.run(rebuild_knowledge_graph(FakeRequest(env, signed_headers(secret))))
    deleted = asyncio.run(delete_knowledge_graph(FakeRequest(env, signed_headers(secret))))
    assert rebuilt.status_code == 409
    assert deleted.status_code == 409
    assert response_json(deleted)["detail"].startswith("Canonical knowledge graph state")
    assert env.APP_DB.connection.execute("SELECT COUNT(*) FROM cf_memories").fetchone()[0] == 1

    legacy_env = environment(secret)
    insert_cutover(legacy_env, state="legacy")
    assert asyncio.run(rebuild_knowledge_graph(FakeRequest(legacy_env, signed_headers(secret)))) == {
        "status": "rebuilding",
        "nodes_count": 0,
        "edges_count": 0,
    }
    assert asyncio.run(delete_knowledge_graph(FakeRequest(legacy_env, signed_headers(secret)))) == {"status": "deleted"}

    missing_env = environment(secret)
    unavailable = asyncio.run(delete_knowledge_graph(FakeRequest(missing_env, signed_headers(secret))))
    assert unavailable.status_code == 503


def test_extract_uses_workers_ai_return_only_and_strictly_validates_graph():
    secret = "graph-secret"
    ai = FakeAi(
        {
            "response": {
                "nodes": [
                    {"id": "user", "label": "User", "node_type": "person", "aliases": []},
                    {"id": "ent_shanghai", "label": "Shanghai", "node_type": "place", "aliases": ["上海"]},
                ],
                "edges": [{"source_id": "user", "target_id": "ent_shanghai", "label": "lives_in"}],
            }
        }
    )
    env = environment(secret, ai=ai)
    insert_cutover(env)
    insert_memory(env, "existing")
    result = asyncio.run(
        extract_knowledge_graph(
            FakeRequest(
                env,
                signed_headers(secret),
                body={"text": "I live in Shanghai.", "include_existing": True},
            )
        )
    )
    shanghai = next(node for node in result["nodes"] if node["id"] == "ent_shanghai")
    assert shanghai["aliases"] == ["上海"]
    assert result["edges"][0]["id"].startswith("edge_")
    assert result["edges"][0]["memory_ids"] == []
    assert "Existing graph" in ai.calls[0][1]["messages"][1]["content"]
    assert ai.calls[0][1]["response_format"]["type"] == "json_schema"
    assert env.APP_DB.connection.execute("SELECT COUNT(*) FROM cf_memories").fetchone()[0] == 1

    invalid_ai = FakeAi({"response": {"nodes": [], "edges": [{"source_id": "x"}]}})
    invalid_env = environment(secret, ai=invalid_ai)
    insert_cutover(invalid_env)
    failed = asyncio.run(
        extract_knowledge_graph(FakeRequest(invalid_env, signed_headers(secret), body={"text": "Some text"}))
    )
    assert failed.status_code == 502
    assert response_json(failed) == {"detail": "knowledge_graph_extract_failed"}
