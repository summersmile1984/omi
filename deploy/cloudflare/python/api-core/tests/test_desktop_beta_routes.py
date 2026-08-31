import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import desktop_beta_routes as beta_routes  # noqa: E402
from desktop_beta_routes import (  # noqa: E402
    BetaAdmissionRequest,
    BetaBreakglassRequest,
    BetaCandidateRequest,
    mutate_beta_breakglass,
    promote_beta_candidate,
    reserve_beta_candidate,
    set_beta_admission,
)


class FakeStatement:
    def __init__(self, connection, sql):
        self.connection = connection
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

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
        for name in (
            "0084_desktop_release_projections.sql",
            "0085_desktop_windows_update_feed.sql",
            "0086_desktop_preview_projections.sql",
            "0089_desktop_release_manifests.sql",
            "0090_desktop_channel_pointers.sql",
            "0092_desktop_beta_admission.sql",
        ):
            self.connection.executescript((migration_dir / name).read_text())

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
        {
            "APP_DB": FakeDb(),
            "ADMIN_KEY": "admin-secret",
            "BETA_PROMOTION_TOKEN": "promote-secret",
        },
    )()


def manifest_payload(release_id: str, *, tier: str = "signed-smoke", passed: bool = False):
    version, build = release_id.removeprefix("v").split("+", 1)
    build_number = int(build.removesuffix("-macos"))
    return {
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
        "qualification_evidence_asset": (
            "desktop-smoke-result-beta.json" if tier == "signed-smoke" else "qualification-evidence-release.json"
        ),
        "qualification_evidence_sha256": "sha256:" + "d" * 64,
        "qualification_tier": tier,
        "qualification_passed": passed,
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


def insert_manifest(env, manifest):
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    env.APP_DB.connection.execute(
        "INSERT INTO cf_desktop_release_manifests (release_id, manifest_json, manifest_sha256, created_at) VALUES (?, ?, ?, ?)",
        (manifest["release_id"], canonical, hashlib.sha256(canonical.encode()).hexdigest(), 1),
    )
    env.APP_DB.connection.commit()


def test_beta_reservation_is_authenticated_idempotent_and_rolls_forward():
    env = make_env()
    request = FakeRequest(env)
    with pytest.raises(HTTPException) as unauthorized:
        asyncio.run(reserve_beta_candidate(request, BetaCandidateRequest(tag="v1.2.3+123-macos")))
    assert unauthorized.value.status_code == 401

    request = FakeRequest(env, {"authorization": "Bearer promote-secret"})
    first = asyncio.run(reserve_beta_candidate(request, BetaCandidateRequest(tag="v1.2.3+123-macos")))
    assert first == {"tag": "v1.2.3+123-macos", "generation": 1}
    assert asyncio.run(reserve_beta_candidate(request, BetaCandidateRequest(tag="v1.2.3+123-macos"))) == first
    second = asyncio.run(reserve_beta_candidate(request, BetaCandidateRequest(tag="v1.2.4+124-macos")))
    assert second == {"tag": "v1.2.4+124-macos", "generation": 2}

    with pytest.raises(HTTPException) as stale:
        asyncio.run(reserve_beta_candidate(request, BetaCandidateRequest(tag="v1.2.5+120-macos")))
    assert stale.value.status_code == 409


def test_admission_requires_reservation_and_admin_key():
    env = make_env()
    admin = FakeRequest(env, {"secret-key": "admin-secret"})
    paused = asyncio.run(set_beta_admission(admin, BetaAdmissionRequest(promotion_enabled=False)))
    assert paused == {"promotion_enabled": False, "generation": 1}
    with pytest.raises(HTTPException) as no_reservation:
        asyncio.run(set_beta_admission(admin, BetaAdmissionRequest(promotion_enabled=True)))
    assert no_reservation.value.status_code == 409

    bearer = FakeRequest(env, {"authorization": "Bearer promote-secret"})
    asyncio.run(reserve_beta_candidate(bearer, BetaCandidateRequest(tag="v1.2.3+123-macos")))
    resumed = asyncio.run(set_beta_admission(admin, BetaAdmissionRequest(promotion_enabled=True)))
    assert resumed == {"promotion_enabled": True, "generation": 3}


def test_promotion_fails_closed_without_github_read_secret():
    env = make_env()
    bearer = FakeRequest(env, {"authorization": "Bearer promote-secret"})
    asyncio.run(reserve_beta_candidate(bearer, BetaCandidateRequest(tag="v1.2.3+123-macos")))
    admin = FakeRequest(env, {"secret-key": "admin-secret"})
    asyncio.run(set_beta_admission(admin, BetaAdmissionRequest(promotion_enabled=True)))

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(promote_beta_candidate(bearer, BetaCandidateRequest(tag="v1.2.3+123-macos")))
    assert rejected.value.status_code == 422
    assert env.APP_DB.connection.execute("SELECT * FROM cf_desktop_channel_pointers").fetchall() == []


def test_signed_promotion_commits_d1_manifest_pointer_and_is_idempotent(monkeypatch):
    env = make_env()
    bearer = FakeRequest(env, {"authorization": "Bearer promote-secret"})
    tag = "v1.2.3+123-macos"
    manifest = manifest_payload(tag)
    monkeypatch.setattr(beta_routes, "build_signed_beta_manifest", lambda *_: _async_value(manifest))

    asyncio.run(reserve_beta_candidate(bearer, BetaCandidateRequest(tag=tag)))
    admin = FakeRequest(env, {"secret-key": "admin-secret"})
    asyncio.run(set_beta_admission(admin, BetaAdmissionRequest(promotion_enabled=True)))
    first = asyncio.run(promote_beta_candidate(bearer, BetaCandidateRequest(tag=tag)))
    assert first == {"tag": tag, "release_id": tag, "generation": 1, "idempotent": False}
    second = asyncio.run(promote_beta_candidate(bearer, BetaCandidateRequest(tag=tag)))
    assert second == {"tag": tag, "release_id": tag, "generation": 1, "idempotent": True}
    assert env.APP_DB.connection.execute("SELECT COUNT(*) FROM cf_desktop_release_manifests").fetchone()[0] == 1


def test_breakglass_rollback_pauses_admission_records_audit_and_supports_two_component_target(monkeypatch):
    env = make_env()
    bearer = FakeRequest(env, {"authorization": "Bearer promote-secret"})
    admin = FakeRequest(env, {"secret-key": "admin-secret"})
    current_tag = "v1.2.3+123-macos"
    target_tag = "v1.2+122-macos"
    current_manifest = manifest_payload(current_tag)
    target_manifest = manifest_payload(target_tag, tier="T2", passed=True)
    monkeypatch.setattr(beta_routes, "build_signed_beta_manifest", lambda *_: _async_value(current_manifest))
    asyncio.run(reserve_beta_candidate(bearer, BetaCandidateRequest(tag=current_tag)))
    asyncio.run(set_beta_admission(admin, BetaAdmissionRequest(promotion_enabled=True)))
    asyncio.run(promote_beta_candidate(bearer, BetaCandidateRequest(tag=current_tag)))
    insert_manifest(env, target_manifest)

    payload = BetaBreakglassRequest(
        operation="rollback",
        current_release_id=current_tag,
        target_release_id=target_tag,
        expected_generation=1,
        actor="release-operator",
        reason="restore known-good signed artifact",
        incident_url="https://github.com/BasedHardware/omi/issues/123",
        request_id="https://github.com/BasedHardware/omi/actions/runs/123/attempts/1",
    )
    receipt = asyncio.run(mutate_beta_breakglass(admin, payload))
    assert receipt == {"operation": "rollback", "release_id": target_tag, "generation": 2}
    pointer = env.APP_DB.connection.execute(
        "SELECT release_id, generation FROM cf_desktop_channel_pointers WHERE platform = 'macos' AND channel = 'beta'"
    ).fetchone()
    assert tuple(pointer) == (target_tag, 2)
    control = env.APP_DB.connection.execute(
        "SELECT promotion_enabled, control_generation FROM cf_desktop_beta_admission WHERE id = 'control'"
    ).fetchone()
    assert tuple(control) == (0, 3)
    assert env.APP_DB.connection.execute("SELECT COUNT(*) FROM cf_desktop_beta_breakglass_audits").fetchone()[0] == 1


def test_breakglass_rollout_requires_incident_recovery_reason():
    env = make_env()
    admin = FakeRequest(env, {"secret-key": "admin-secret"})
    payload = BetaBreakglassRequest(
        operation="rollout",
        current_release_id="v1.2.3+123-macos",
        target_release_id="v1.2.4+124-macos",
        expected_generation=1,
        actor="release-operator",
        reason="emergency",
        incident_url="https://github.com/BasedHardware/omi/issues/123",
        request_id="https://github.com/BasedHardware/omi/actions/runs/123/attempts/2",
    )
    with pytest.raises(HTTPException) as missing:
        asyncio.run(mutate_beta_breakglass(admin, payload))
    assert missing.value.status_code == 422


async def _async_value(value):
    return value
