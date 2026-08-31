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
    DesktopPreviewDelistRequest,
    DesktopPreviewPublishRequest,
    delist_desktop_preview,
    get_desktop_release_manifest,
    promote_desktop_channel,
    publish_desktop_preview,
    register_desktop_release_manifest,
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

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0084_desktop_release_projections.sql").read_text())
        self.connection.executescript((migration_dir / "0085_desktop_windows_update_feed.sql").read_text())
        self.connection.executescript((migration_dir / "0086_desktop_preview_projections.sql").read_text())
        self.connection.executescript((migration_dir / "0089_desktop_release_manifests.sql").read_text())
        self.connection.executescript((migration_dir / "0090_desktop_channel_pointers.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        return [await statement.run() for statement in statements]


class FakeRequest:
    def __init__(self, env, headers=None):
        self.scope = {"env": env}
        self.headers = headers or {}


def make_env():
    return type(
        "Env",
        (),
        {"APP_DB": FakeDb(), "ADMIN_KEY": "admin-secret", "DESKTOP_PREVIEW_PUBLISH_KEY": "preview-secret"},
    )()


def desktop_manifest_payload(*, release_id="v0.12.64+12064-macos", notes=None):
    version, build = release_id.removeprefix("v").split("+", 1)
    build_number = int(build.removesuffix("-macos"))
    manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "platform": "macos",
        "version": version,
        "build_number": build_number,
        "app_source_sha": "a" * 40,
        "zip_url": f"https://github.com/BasedHardware/omi/releases/download/{release_id}/Omi.zip",
        "zip_sha256": "sha256:" + "b" * 64,
        "dmg_url": f"https://github.com/BasedHardware/omi/releases/download/{release_id}/omi.dmg",
        "dmg_sha256": "sha256:" + "c" * 64,
        "ed_signature": "sparkle-signature",
        "qualification_evidence_asset": f"qualification-evidence-{release_id}.json",
        "qualification_evidence_sha256": "sha256:" + "d" * 64,
        "qualification_tier": "T2",
        "qualification_passed": True,
        "backend_mode": "app_only",
        "compatibility_contract": {
            "schema_version": 1,
            "app_release_id": release_id,
            "app_version": version,
            "app_build_number": build_number,
            "backend_mode": "app_only",
            "environment_contract_version": "desktop-backend-env-v1",
        },
        "environment_contract_version": "desktop-backend-env-v1",
        "created_at": "2026-08-31T00:00:00Z",
        "published_at": "2026-08-31T00:00:00Z",
        "changelog": ["Qualified release"],
        "mandatory": False,
    }
    if notes is not None:
        manifest["changelog"] = [notes]
    return manifest


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


def preview_payload(*, slug="feature-demo", source_sha="a" * 40, notes="Ready for review", expected_generation=None):
    preview_id = "p" + hashlib.sha256(slug.encode()).hexdigest()[:10]
    payload = {
        "slug": slug,
        "source_sha": source_sha,
        "dmg_url": f"https://storage.googleapis.com/omi_macos_updates/previews/{slug}/{source_sha}/Omi-Preview.dmg",
        "dmg_sha256": "b" * 64,
        "app_name": "Omi Preview Feature Demo",
        "bundle_id": f"com.omi.preview.{preview_id}",
        "url_scheme": f"omi-preview-{preview_id}",
        "built_at": "2026-08-31T00:00:00Z",
        "signer": "ci@example.invalid",
        "notarization": "stapled",
        "notes": notes,
        "backend_url": "https://api.example.invalid",
    }
    if expected_generation is not None:
        payload["expected_generation"] = expected_generation
    return payload


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


def test_desktop_preview_delist_requires_key_and_compare_deletes_only_pointer():
    env = make_env()
    insert_preview(env)

    with pytest.raises(HTTPException) as unauthorized:
        asyncio.run(
            delist_desktop_preview(
                FakeRequest(env),
                "feature-demo",
                DesktopPreviewDelistRequest(expected_generation=1),
            )
        )
    assert unauthorized.value.status_code == 403

    payload = DesktopPreviewDelistRequest(expected_generation=0)
    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(delist_desktop_preview(FakeRequest(env, {"secret-key": "preview-secret"}), "feature-demo", payload))
    assert mismatch.value.status_code == 409
    assert "generation mismatch" in str(mismatch.value.detail)

    deleted = asyncio.run(
        delist_desktop_preview(
            FakeRequest(env, {"secret-key": "preview-secret"}),
            "feature-demo",
            DesktopPreviewDelistRequest(expected_generation=1),
        )
    )
    assert deleted == {"success": True, "slug": "feature-demo", "deleted": True, "generation": 1}
    assert (
        env.APP_DB.connection.execute(
            "SELECT COUNT(*) FROM cf_desktop_preview_pointers WHERE slug = 'feature-demo'"
        ).fetchone()[0]
        == 0
    )
    assert (
        env.APP_DB.connection.execute(
            "SELECT COUNT(*) FROM cf_desktop_preview_manifests WHERE slug = 'feature-demo'"
        ).fetchone()[0]
        == 1
    )


def test_desktop_preview_delist_missing_pointer_is_idempotent_with_valid_key():
    env = make_env()
    result = asyncio.run(
        delist_desktop_preview(
            FakeRequest(env, {"secret-key": "preview-secret"}),
            "missing",
            DesktopPreviewDelistRequest(expected_generation=0),
        )
    )
    assert result == {"success": True, "slug": "missing", "deleted": False, "generation": None}


def test_desktop_preview_publish_projects_manifest_and_pointer_with_idempotent_retry():
    env = make_env()
    payload = DesktopPreviewPublishRequest.model_validate(preview_payload(expected_generation=0))
    request = FakeRequest(env, {"secret-key": "preview-secret"})

    published = asyncio.run(publish_desktop_preview(request, payload))
    assert published["success"] is True
    assert published["manifest"]["source_sha"] == "a" * 40
    assert published["pointer"] == {"slug": "feature-demo", "source_sha": "a" * 40, "generation": 1}

    retry = asyncio.run(
        publish_desktop_preview(
            request,
            DesktopPreviewPublishRequest.model_validate(preview_payload(expected_generation=1)),
        )
    )
    assert retry["pointer"] == published["pointer"]
    assert (
        env.APP_DB.connection.execute(
            "SELECT COUNT(*) FROM cf_desktop_preview_manifests WHERE slug = 'feature-demo'"
        ).fetchone()[0]
        == 1
    )


def test_desktop_preview_publish_rejects_stale_generation_and_immutable_conflict():
    env = make_env()
    request = FakeRequest(env, {"secret-key": "preview-secret"})
    asyncio.run(
        publish_desktop_preview(
            request,
            DesktopPreviewPublishRequest.model_validate(preview_payload(expected_generation=0)),
        )
    )
    with pytest.raises(HTTPException) as stale:
        asyncio.run(
            publish_desktop_preview(
                request,
                DesktopPreviewPublishRequest.model_validate(
                    preview_payload(source_sha="c" * 40, expected_generation=0)
                ),
            )
        )
    assert stale.value.status_code == 409
    assert "generation mismatch" in str(stale.value.detail)

    with pytest.raises(HTTPException) as conflict:
        asyncio.run(
            publish_desktop_preview(
                request,
                DesktopPreviewPublishRequest.model_validate(preview_payload(notes="changed", expected_generation=1)),
            )
        )
    assert conflict.value.status_code == 409
    assert "immutable metadata" in str(conflict.value.detail)


def test_desktop_release_manifest_registers_idempotently_and_returns_canonical_digest():
    env = make_env()
    request = FakeRequest(env, {"secret-key": "admin-secret"})
    payload = desktop_manifest_payload()

    created = asyncio.run(register_desktop_release_manifest(request, payload))
    assert created == {"success": True, "manifest": payload}
    retry = asyncio.run(register_desktop_release_manifest(request, payload))
    assert retry == created

    read = asyncio.run(get_desktop_release_manifest(request, payload["release_id"]))
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert read == {
        "success": True,
        "manifest": payload,
        "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
    }
    with pytest.raises(sqlite3.IntegrityError):
        env.APP_DB.connection.execute(
            "UPDATE cf_desktop_release_manifests SET manifest_json = ? WHERE release_id = ?",
            ("{}", payload["release_id"]),
        )
    with pytest.raises(sqlite3.IntegrityError):
        env.APP_DB.connection.execute(
            "DELETE FROM cf_desktop_release_manifests WHERE release_id = ?",
            (payload["release_id"],),
        )


def test_desktop_release_manifest_requires_admin_key_and_rejects_immutable_conflicts():
    env = make_env()
    payload = desktop_manifest_payload()
    with pytest.raises(HTTPException) as unauthorized:
        asyncio.run(register_desktop_release_manifest(FakeRequest(env), payload))
    assert unauthorized.value.status_code == 403

    request = FakeRequest(env, {"secret-key": "admin-secret"})
    asyncio.run(register_desktop_release_manifest(request, payload))
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(register_desktop_release_manifest(request, desktop_manifest_payload(notes="changed")))
    assert conflict.value.status_code == 409
    assert "immutable metadata" in str(conflict.value.detail)


def test_desktop_release_manifest_fails_closed_for_missing_or_corrupt_projection():
    env = make_env()
    request = FakeRequest(env, {"secret-key": "admin-secret"})
    release_id = "v0.12.64+12064-macos"
    with pytest.raises(HTTPException) as missing:
        asyncio.run(get_desktop_release_manifest(request, release_id))
    assert missing.value.status_code == 404

    env.APP_DB.connection.execute(
        "INSERT INTO cf_desktop_release_manifests (release_id, manifest_json, manifest_sha256, created_at) "
        "VALUES (?, ?, ?, 1)",
        (release_id, "{}", "0" * 64),
    )
    env.APP_DB.connection.commit()
    with pytest.raises(HTTPException) as corrupt:
        asyncio.run(get_desktop_release_manifest(request, release_id))
    assert corrupt.value.status_code == 503

    with pytest.raises(HTTPException) as malformed_id:
        asyncio.run(get_desktop_release_manifest(request, "not-a-release"))
    assert malformed_id.value.status_code == 404


def test_stable_channel_promotion_requires_retained_manifest_and_uses_generation_cas():
    env = make_env()
    payload = desktop_manifest_payload()
    request = FakeRequest(env, {"secret-key": "admin-secret"})

    assert asyncio.run(register_desktop_release_manifest(request, payload))["success"] is True
    promoted = asyncio.run(
        promote_desktop_channel(
            request,
            {"platform": "macos", "channel": "stable", "release_id": payload["release_id"]},
        )
    )
    assert promoted["pointer"]["generation"] == 1
    assert promoted["idempotent"] is False

    # An exact retry is idempotent even when a caller repeats stale CAS inputs.
    retry = asyncio.run(
        promote_desktop_channel(
            request,
            {
                "platform": "macos",
                "channel": "stable",
                "release_id": payload["release_id"],
                "expected_generation": 99,
            },
        )
    )
    assert retry["idempotent"] is True

    newer = desktop_manifest_payload(release_id="v0.12.65+12065-macos")
    asyncio.run(register_desktop_release_manifest(request, newer))
    with pytest.raises(HTTPException) as stale:
        asyncio.run(
            promote_desktop_channel(
                request,
                {
                    "platform": "macos",
                    "channel": "stable",
                    "release_id": newer["release_id"],
                    "expected_generation": 0,
                },
            )
        )
    assert stale.value.status_code == 409
    assert "generation mismatch" in str(stale.value.detail)

    advanced = asyncio.run(
        promote_desktop_channel(
            request,
            {
                "platform": "macos",
                "channel": "stable",
                "release_id": newer["release_id"],
                "expected_generation": 1,
                "expected_current_release_id": payload["release_id"],
            },
        )
    )
    assert advanced["pointer"]["release_id"] == newer["release_id"]
    assert advanced["pointer"]["generation"] == 2


def test_stable_channel_promotion_fails_closed_for_missing_manifest_and_invalid_scope():
    env = make_env()
    request = FakeRequest(env, {"secret-key": "admin-secret"})
    with pytest.raises(HTTPException) as missing:
        asyncio.run(
            promote_desktop_channel(
                request,
                {"platform": "macos", "channel": "stable", "release_id": "v0.12.99+1299-macos"},
            )
        )
    assert missing.value.status_code == 409

    with pytest.raises(HTTPException) as invalid_scope:
        asyncio.run(
            promote_desktop_channel(
                request,
                {"platform": "windows", "channel": "stable", "release_id": "v0.12.99+1299-macos"},
            )
        )
    assert invalid_scope.value.status_code == 409


def test_stable_pointer_becomes_authority_for_mac_release_feeds():
    env = make_env()
    request = FakeRequest(env, {"secret-key": "admin-secret"})
    manifest = desktop_manifest_payload(release_id="v0.12.70+12070-macos")
    asyncio.run(register_desktop_release_manifest(request, manifest))
    # A stale legacy row must not win once the explicit Stable pointer exists.
    insert_release(env, release_id="legacy", version="0.1.0", build_number=1, channel="stable")
    asyncio.run(
        promote_desktop_channel(
            request,
            {"platform": "macos", "channel": "stable", "release_id": manifest["release_id"]},
        )
    )

    latest = asyncio.run(get_latest_version(FakeRequest(env)))
    assert latest["version"] == manifest["version"]
    assert latest["build_number"] == manifest["build_number"]
    assert latest["download_url"] == manifest["zip_url"]
    redirect = asyncio.run(download_redirect(FakeRequest(env)))
    assert redirect.headers["location"] == manifest["dmg_url"]
