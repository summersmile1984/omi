import asyncio
import json
import sqlite3
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from desktop_release_routes import (  # noqa: E402
    get_appcast,
    get_latest_version,
    get_update_policy,
    download_redirect,
)


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

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row is not None else None


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0084_desktop_release_projections.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeRequest:
    def __init__(self, env):
        self.scope = {"env": env}


def make_env():
    return type("Env", (), {"APP_DB": FakeDb()})()


def insert_release(env, *, release_id, version, build_number, channel, is_live=1, manual_download_url=None):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_desktop_releases "
        "(id, version, build_number, download_url, manual_download_url, ed_signature, published_at, "
        "changelog_json, is_live, is_critical, channel, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            release_id,
            version,
            build_number,
            f"https://downloads.example/{release_id}.zip",
            manual_download_url,
            "sig-<safe>",
            "2026-08-31T00:00:00Z",
            json.dumps(["fix <escaping>", "close ]]> marker"]),
            is_live,
            1,
            channel,
            1,
            1,
        ),
    )
    env.APP_DB.connection.commit()


def test_empty_projection_keeps_feed_usable_but_fails_closed_for_downloads():
    env = make_env()
    request = FakeRequest(env)
    feed = asyncio.run(get_appcast(request, platform="macos"))
    assert feed.status_code == 200
    assert feed.media_type == "application/xml"
    assert "<item>" not in feed.body.decode()

    with pytest.raises(HTTPException) as latest_error:
        asyncio.run(get_latest_version(request))
    assert latest_error.value.status_code == 404
    with pytest.raises(HTTPException) as download_error:
        asyncio.run(download_redirect(request))
    assert download_error.value.status_code == 404


def test_latest_and_download_choose_highest_live_stable_release_only():
    env = make_env()
    insert_release(env, release_id="beta", version="9.9.9", build_number=99, channel="beta")
    insert_release(env, release_id="old", version="1.0.0", build_number=10, channel="stable")
    insert_release(
        env,
        release_id="new",
        version="1.1.0",
        build_number=11,
        channel="stable",
        manual_download_url="https://downloads.example/new.dmg",
    )
    request = FakeRequest(env)
    latest = asyncio.run(get_latest_version(request))
    assert latest == {
        "version": "1.1.0",
        "build_number": 11,
        "download_url": "https://downloads.example/new.zip",
        "is_critical": True,
    }
    redirect = asyncio.run(download_redirect(request))
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "https://downloads.example/new.dmg"


def test_download_derives_dmg_when_release_only_publishes_omi_zip():
    env = make_env()
    insert_release(env, release_id="zip", version="1.2.0", build_number=12, channel="stable")
    env.APP_DB.connection.execute(
        "UPDATE cf_desktop_releases SET download_url = ? WHERE id = ?",
        ("https://downloads.example/path/Omi.zip", "zip"),
    )
    env.APP_DB.connection.commit()
    redirect = asyncio.run(download_redirect(FakeRequest(env)))
    assert redirect.headers["location"] == "https://downloads.example/path/Omi.dmg"


def test_appcast_escapes_metadata_and_keeps_one_item_per_channel():
    env = make_env()
    insert_release(env, release_id="stable-new", version="2&", build_number=20, channel="stable")
    insert_release(env, release_id="stable-old", version="1.0", build_number=19, channel="stable")
    insert_release(env, release_id="beta", version="3.0", build_number=30, channel="beta")
    xml = asyncio.run(get_appcast(FakeRequest(env), platform="windows")).body.decode()
    assert xml.count("<item>") == 2
    assert "Omi 2&amp;" in xml
    assert "sparkle:os=\"windows\"" in xml
    assert "sparkle:channel>beta" in xml
    assert "&lt;escaping&gt;" in xml
    assert "]]>&gt;" not in xml


def test_update_policy_applies_platform_and_maximum_build_guards():
    env = make_env()
    env.APP_DB.connection.execute(
        "UPDATE cf_desktop_update_policy SET active = 1, severity = 'required', maximum_build_number = 42, "
        "latest_build_number = 50, title = ?, message = ?, platforms_json = ?, download_url = ? WHERE id = 'current'",
        ("Update now", "Security fix", json.dumps(["macos"]), "https://downloads.example/latest.dmg"),
    )
    env.APP_DB.connection.commit()
    request = FakeRequest(env)
    active = asyncio.run(get_update_policy(request, platform="macos", current_build=40))
    assert active["active"] is True
    assert active["severity"] == "required"
    assert active["platforms"] == ["macos"]
    assert active["download_url"] == "https://downloads.example/latest.dmg"

    assert asyncio.run(get_update_policy(request, platform="windows", current_build=40))["active"] is False
    assert asyncio.run(get_update_policy(request, platform="macos", current_build=43))["active"] is False
