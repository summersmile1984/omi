import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from desktop_release_routes import (  # noqa: E402
    download_beta_desktop,
    download_latest_desktop,
    download_windows_desktop,
    get_desktop_appcast,
    get_appcast,
    get_latest_version,
    get_update_policy,
    download_redirect,
    get_windows_update_feed,
    download_current_desktop_preview,
    download_immutable_desktop_preview,
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
        self.connection.executescript((migration_dir / "0085_desktop_windows_update_feed.sql").read_text())
        self.connection.executescript((migration_dir / "0086_desktop_preview_projections.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeRequest:
    def __init__(self, env):
        self.scope = {"env": env}


def make_env():
    return type("Env", (), {"APP_DB": FakeDb()})()


def insert_release(
    env,
    *,
    release_id,
    version,
    build_number,
    channel,
    is_live=1,
    manual_download_url=None,
    windows_feed_url=None,
):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_desktop_releases "
        "(id, version, build_number, download_url, manual_download_url, windows_feed_url, ed_signature, published_at, "
        "changelog_json, is_live, is_critical, channel, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            release_id,
            version,
            build_number,
            f"https://downloads.example/{release_id}.zip",
            manual_download_url,
            windows_feed_url,
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


def insert_preview(env, *, slug="feature-demo", source_sha="a" * 40, notes="Ready for review"):
    preview_id = "p" + hashlib.sha256(slug.encode()).hexdigest()[:10]
    env.APP_DB.connection.execute(
        "INSERT INTO cf_desktop_preview_manifests "
        "(slug, source_sha, dmg_url, dmg_sha256, app_name, bundle_id, url_scheme, built_at, signer, "
        "notarization, notes, backend_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            slug,
            source_sha,
            f"https://storage.googleapis.com/omi_macos_updates/previews/{slug}/{source_sha}/Omi-Preview.dmg",
            "b" * 64,
            "Omi Preview Feature Demo",
            f"com.omi.preview.{preview_id}",
            f"omi-preview-{preview_id}",
            "2026-08-31T00:00:00Z",
            "ci@example.invalid",
            "stapled",
            notes,
            "https://api.example.invalid",
            1,
        ),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_desktop_preview_pointers (slug, source_sha, generation, updated_at) VALUES (?, ?, ?, ?)",
        (slug, source_sha, 1, 1),
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


def test_v2_appcast_is_identity_strict_and_download_landing_pages_are_channel_scoped():
    env = make_env()
    insert_release(env, release_id="stable", version="2.0.0", build_number=20, channel="stable")
    insert_release(env, release_id="beta", version="2.1.0-beta", build_number=21, channel="beta")
    request = FakeRequest(env)

    stable_xml = asyncio.run(get_desktop_appcast(request, platform="macos", identity="stable"))
    beta_xml = asyncio.run(get_desktop_appcast(request, platform="macos", identity="beta"))
    assert stable_xml.status_code == 200
    assert stable_xml.body.decode().count("<item>") == 1
    assert "sparkle:channel" not in stable_xml.body.decode()
    assert beta_xml.body.decode().count("<item>") == 1
    assert "sparkle:channel>beta" in beta_xml.body.decode()

    latest = asyncio.run(download_latest_desktop(request, platform="macos", channel="stable"))
    assert latest.status_code == 200
    assert "Omi for macOS" in latest.body.decode()
    assert "https://downloads.example/stable.zip" in latest.body.decode()
    beta = asyncio.run(download_beta_desktop(request, platform="macos"))
    assert beta.status_code == 200
    assert "Omi Beta for macOS" in beta.body.decode()
    windows = asyncio.run(download_windows_desktop(request, channel="stable"))
    assert windows.status_code == 200
    assert "for Windows" in windows.body.decode()


def test_v2_windows_download_falls_back_to_beta_when_stable_is_empty():
    env = make_env()
    insert_release(env, release_id="beta", version="3.0.0-beta", build_number=30, channel="beta")
    response = asyncio.run(download_windows_desktop(FakeRequest(env), channel="stable"))
    body = response.body.decode()
    assert response.status_code == 200
    assert "No stable build is published" in body
    assert "3.0.0-beta" in body


def test_windows_update_feed_requires_explicit_url_and_allows_beta_to_stable_fallback(capsys):
    env = make_env()
    request = FakeRequest(env)
    with pytest.raises(HTTPException) as empty_error:
        asyncio.run(get_windows_update_feed(request, channel="stable"))
    assert empty_error.value.status_code == 404

    insert_release(
        env,
        release_id="stable",
        version="4.0.0",
        build_number=40,
        channel="stable",
        windows_feed_url="https://downloads.example/windows/v4.0.0/",
    )
    beta = asyncio.run(get_windows_update_feed(request, channel="beta"))
    assert beta.status_code == 200
    assert json.loads(beta.body) == {
        "requested_channel": "beta",
        "served_channel": "stable",
        "version": "4.0.0",
        "feed_url": "https://downloads.example/windows/v4.0.0/",
    }
    assert beta.headers["cache-control"] == "no-store"
    assert '"event":"fallback"' in capsys.readouterr().out

    env.APP_DB.connection.execute(
        "UPDATE cf_desktop_releases SET windows_feed_url = ? WHERE id = ?",
        ("https://downloads.example/windows/beta/", "stable"),
    )
    env.APP_DB.connection.commit()
    stable = asyncio.run(get_windows_update_feed(request, channel="stable"))
    stable_payload = json.loads(stable.body)
    assert stable_payload["served_channel"] == "stable"
    assert stable_payload["feed_url"].endswith("/beta/")


def test_windows_update_feed_does_not_infer_url_from_installer():
    env = make_env()
    insert_release(
        env,
        release_id="stable",
        version="4.0.0",
        build_number=40,
        channel="stable",
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(get_windows_update_feed(FakeRequest(env), channel="stable"))
    assert error.value.status_code == 404


def test_desktop_preview_routes_serve_current_and_immutable_manifest_with_escaped_notes():
    env = make_env()
    insert_preview(env, notes='<script>alert("x")</script>')
    request = FakeRequest(env)

    current = asyncio.run(download_current_desktop_preview(request, "feature-demo"))
    assert current.status_code == 200
    assert current.headers["cache-control"] == "no-store"
    body = current.body.decode()
    assert "Omi Preview Feature Demo" in body
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in body
    assert "https://storage.googleapis.com/omi_macos_updates/previews/feature-demo/" in body

    immutable = asyncio.run(download_immutable_desktop_preview(request, "feature-demo", "a" * 40))
    assert immutable.status_code == 200
    assert immutable.body == current.body


def test_desktop_preview_routes_fail_closed_for_missing_or_malformed_projection():
    env = make_env()
    request = FakeRequest(env)
    with pytest.raises(HTTPException) as missing:
        asyncio.run(download_current_desktop_preview(request, "missing"))
    assert missing.value.status_code == 404

    insert_preview(env)
    env.APP_DB.connection.execute(
        "UPDATE cf_desktop_preview_manifests SET dmg_url = ? WHERE slug = ?",
        ("https://downloads.example/not-canonical.dmg", "feature-demo"),
    )
    env.APP_DB.connection.commit()
    with pytest.raises(HTTPException) as malformed:
        asyncio.run(download_current_desktop_preview(request, "feature-demo"))
    assert malformed.value.status_code == 404
