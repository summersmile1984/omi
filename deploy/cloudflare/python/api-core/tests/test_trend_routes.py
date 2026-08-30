import asyncio
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from trend_routes import get_trends  # noqa: E402


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[3] / "migrations/app/0082_trends.sql"
        self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeStatement:
    def __init__(self, connection, sql):
        self.connection = connection
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}


class FakeRequest:
    def __init__(self, env):
        self.scope = {"env": env}


def test_trends_are_grouped_and_topics_are_sorted_without_memory_ids():
    env = type("Env", (), {"APP_DB": FakeDb()})()
    env.APP_DB.connection.executemany(
        "INSERT INTO cf_trend_categories (id, category, type, created_at) VALUES (?, ?, ?, ?)",
        [
            ("cat-company", "company", "best", 20),
            ("cat-ceo", "ceo", "worst", 30),
        ],
    )
    env.APP_DB.connection.executemany(
        "INSERT INTO cf_trend_topics (category_id, id, topic, memories_count) VALUES (?, ?, ?, ?)",
        [
            ("cat-company", "topic-low", "Figma", 1),
            ("cat-company", "topic-high", "OpenAI", 4),
        ],
    )
    env.APP_DB.connection.commit()

    response = asyncio.run(get_trends(FakeRequest(env)))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60"
    assert response.body is not None
    assert json.loads(response.body) == [
        {
            "id": "cat-ceo",
            "category": "ceo",
            "type": "worst",
            "created_at": "1970-01-01T00:00:30+00:00",
            "topics": [],
        },
        {
            "id": "cat-company",
            "category": "company",
            "type": "best",
            "created_at": "1970-01-01T00:00:20+00:00",
            "topics": [
                {"id": "topic-high", "topic": "OpenAI", "memories_count": 4},
                {"id": "topic-low", "topic": "Figma", "memories_count": 1},
            ],
        },
    ]


def test_trends_fail_closed_when_d1_is_unavailable():
    env = type("Env", (), {"APP_DB": object()})()
    response = asyncio.run(get_trends(FakeRequest(env)))
    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "trends unavailable"}
