"""Prove the fork's patches still match upstream's real symbols.

This is the test that fails on the sync where upstream renames or moves a
patched function. It resolves every declared seam against the actual upstream
modules -- without swapping anything -- so a rename surfaces as a named failure
here instead of as a self-hosted deployment quietly calling Google Cloud.
"""

from __future__ import annotations

import unittest

from fork.patches import collect
from fork.registry import PatchError, build_registry

SELF_HOSTED = {
    "name": "self_hosted.production",
    "target": "self_hosted",
    "data_plane": {"object_store": "minio", "queue": "redis", "store": "firestore_pg"},
}
CLOUDFLARE = {
    "name": "cloudflare.production",
    "target": "cloudflare",
    "data_plane": {"object_store": "r2", "queue": "cf_queues", "store": "d1"},
}
UPSTREAM = {
    "name": "omi_cloud.production",
    "target": "omi_cloud",
    "data_plane": {"object_store": "gcs", "queue": "cloud_tasks", "store": "firestore"},
}


class RealSeamTests(unittest.TestCase):
    def test_every_self_hosted_seam_resolves_against_upstream(self):
        try:
            build_registry(collect()).verify(SELF_HOSTED)
        except PatchError as error:
            self.fail(
                f"a fork patch no longer matches upstream: {error}\n"
                f"Fix backend/fork/patches/, do not edit the upstream module."
            )

    def test_cloudflare_profile_shares_no_backend_patches(self):
        # The Cloudflare target does not run this Python backend at all; its
        # data plane is served by Workers. Any patch claiming to apply there
        # would mean a seam was gated on the wrong condition.
        registry = build_registry(collect())
        applicable = [p.name for p in registry.patches if p.applies_to(CLOUDFLARE)]
        self.assertEqual(applicable, [], f"unexpected backend patches for cloudflare: {applicable}")

    def test_upstream_profile_activates_no_patch(self):
        registry = build_registry(collect())
        applicable = [p.name for p in registry.patches if p.applies_to(UPSTREAM)]
        self.assertEqual(applicable, [], f"patches must not apply in upstream mode: {applicable}")

    def test_every_patch_declares_a_reason(self):
        for patch in build_registry(collect()).patches:
            self.assertTrue(patch.reason.strip(), f"{patch.name} has no reason")


if __name__ == "__main__":
    unittest.main(verbosity=2)
