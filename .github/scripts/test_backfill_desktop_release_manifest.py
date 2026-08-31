#!/usr/bin/env python3
"""Tests for the Firestore-to-Cloudflare desktop manifest projection helper."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import unittest


SCRIPT_DIR = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("backfill_desktop_release_manifest", SCRIPT_DIR / "backfill-desktop-release-manifest.py")
assert SPEC and SPEC.loader
backfill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backfill)


def manifest() -> dict[str, object]:
    release_id = "v1.2.3+10203-macos"
    return {
        "schema_version": 1,
        "release_id": release_id,
        "platform": "macos",
        "version": "1.2.3",
        "build_number": 10203,
        "app_source_sha": "a" * 40,
        "zip_url": f"https://github.com/BasedHardware/omi/releases/download/{release_id}/Omi.zip",
        "zip_sha256": "sha256:" + "b" * 64,
        "dmg_url": f"https://github.com/BasedHardware/omi/releases/download/{release_id}/omi.dmg",
        "dmg_sha256": "sha256:" + "c" * 64,
        "ed_signature": "ed-signature",
        "qualification_evidence_asset": f"qualification-evidence-{release_id}.json",
        "qualification_evidence_sha256": "sha256:" + "d" * 64,
        "qualification_tier": "T2",
        "qualification_passed": True,
        "backend_mode": "app_only",
        "compatibility_contract": {
            "schema_version": 1,
            "app_release_id": release_id,
            "app_version": "1.2.3",
            "app_build_number": 10203,
            "backend_mode": "app_only",
            "environment_contract_version": "desktop-backend-env-v1",
        },
        "environment_contract_version": "desktop-backend-env-v1",
        "created_at": "2026-08-31T00:00:00Z",
    }


class Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class Opener:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, request: object, *, timeout: int) -> Response:
        self.requests.append((request, timeout))
        return Response(next(self.responses))


class BackfillDesktopReleaseManifestTests(unittest.TestCase):
    def test_backfill_reads_legacy_and_posts_the_same_validated_manifest(self) -> None:
        release = manifest()
        digest = backfill._manifest_digest(release)
        opener = Opener(
            [
                {"success": True, "manifest": release, "manifest_sha256": digest},
                {"success": True, "manifest": release, "manifest_sha256": digest},
            ]
        )

        result = backfill.backfill_manifest(
            release["release_id"],
            legacy_base_url="https://legacy.example",
            target_base_url="https://edge.example",
            admin_key="secret",
            opener=opener,
        )

        self.assertEqual(result["release_id"], release["release_id"])
        self.assertEqual(result["manifest_sha256"], digest)
        self.assertEqual(len(opener.requests), 2)
        legacy_request, target_request = (entry[0] for entry in opener.requests)
        self.assertEqual(legacy_request.full_url, "https://legacy.example/v2/desktop/releases/v1.2.3%2B10203-macos")
        self.assertEqual(target_request.full_url, "https://edge.example/v2/desktop/releases")
        self.assertEqual(legacy_request.get_header("Secret-key"), "secret")
        self.assertEqual(target_request.get_header("Secret-key"), "secret")
        self.assertEqual(json.loads(target_request.data), release)

    def test_rejects_a_legacy_digest_mismatch_before_posting(self) -> None:
        release = manifest()
        opener = Opener([{"success": True, "manifest": release, "manifest_sha256": "0" * 64}])

        with self.assertRaisesRegex(backfill.ManifestBackfillError, "digest"):
            backfill.backfill_manifest(
                release["release_id"],
                legacy_base_url="https://legacy.example",
                target_base_url="https://edge.example",
                admin_key="secret",
                opener=opener,
            )
        self.assertEqual(len(opener.requests), 1)

    def test_requires_https_endpoints_and_never_accepts_embedded_credentials(self) -> None:
        release = manifest()
        for base_url in ("http://edge.example", "https://user:pass@edge.example", "https://edge.example?x=1"):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                backfill.publish_cloudflare_manifest(
                    release,
                    target_base_url=base_url,
                    admin_key="secret",
                    opener=Opener([]),
                )

    def test_rejects_a_cloudflare_response_that_mutates_the_manifest(self) -> None:
        release = manifest()
        changed = dict(release, ed_signature="changed")
        opener = Opener([{"success": True, "manifest": changed}])

        with self.assertRaisesRegex(backfill.ManifestBackfillError, "differs"):
            backfill.publish_cloudflare_manifest(
                release,
                target_base_url="https://edge.example",
                admin_key="secret",
                opener=opener,
            )

    def test_requires_explicit_success_acknowledgement_from_both_endpoints(self) -> None:
        release = manifest()
        opener = Opener([{"manifest": release}])

        with self.assertRaisesRegex(backfill.ManifestBackfillError, "acknowledge success"):
            backfill.fetch_legacy_manifest(
                release["release_id"],
                legacy_base_url="https://legacy.example",
                admin_key="secret",
                opener=opener,
            )

    def test_builds_a_content_bound_dry_run_plan_without_target_writes(self) -> None:
        release = manifest()
        plan = backfill.build_dry_run_plan(
            release,
            legacy_endpoint="https://legacy.example/v2/desktop/releases/v1.2.3%2B10203-macos",
        )

        self.assertEqual(plan["mode"], "dry-run")
        self.assertEqual(plan["manifest"], release)
        self.assertEqual(plan["manifest_sha256"], backfill._manifest_digest(release))
        self.assertEqual(plan["source"]["manifest_sha256"], plan["manifest_sha256"])
        expected_plan_hash = hashlib.sha256(
            backfill._stable_json(
                {
                    "schema_version": 1,
                    "source": plan["source"],
                    "manifest_sha256": plan["manifest_sha256"],
                }
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(plan["plan_hash"], expected_plan_hash)

    def test_apply_reviewed_plan_uses_the_gated_executor_and_never_prints_the_key(self) -> None:
        release = manifest()
        plan = backfill.build_dry_run_plan(
            release,
            legacy_endpoint="https://legacy.example/v2/desktop/releases/v1.2.3%2B10203-macos",
        )
        opener = Opener(
            [
                {"review_id": "00000000-0000-4000-8000-000000000000"},
                {
                    "status": "applied",
                    "release_id": release["release_id"],
                    "manifest_sha256": plan["manifest_sha256"],
                    "applied_count": 1,
                    "already_applied_count": 0,
                },
            ]
        )

        result = backfill.apply_reviewed_plan(
            plan,
            target_base_url="https://edge.example",
            admin_key="secret",
            opener=opener,
        )

        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(opener.requests), 2)
        review_request, apply_request = (entry[0] for entry in opener.requests)
        self.assertEqual(review_request.full_url, "https://edge.example/internal/desktop-release-history/reviews")
        self.assertIn("/00000000-0000-4000-8000-000000000000/apply", apply_request.full_url)
        self.assertEqual(review_request.get_header("Secret-key"), "secret")
        self.assertEqual(json.loads(review_request.data), plan)
        self.assertNotIn("secret", json.dumps(plan))

        opener = Opener([{"success": True, "manifest": release}, {"manifest": release}])
        with self.assertRaisesRegex(backfill.ManifestBackfillError, "acknowledge success"):
            backfill.backfill_manifest(
                release["release_id"],
                legacy_base_url="https://legacy.example",
                target_base_url="https://edge.example",
                admin_key="secret",
                opener=opener,
            )


if __name__ == "__main__":
    unittest.main()
