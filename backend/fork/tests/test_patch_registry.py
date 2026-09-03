"""Behavioral tests for the fork's import-time patch registry.

The property that matters most is the first one: in upstream mode the fork must
change nothing. Everything else in this plan rests on being able to run
upstream's own suite unmodified and have it mean something.

The registry is exercised against synthetic modules rather than upstream's, so
these tests stay fast, hermetic, and independent of whichever seams exist today.
Whether the real seams still resolve is checked separately by
`test_real_seams.py`, which is the test that fails when upstream renames one.
"""

from __future__ import annotations

import sys
import types
import unittest

from fork.registry import Patch, PatchError, build_registry


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


SELF_HOSTED = {
    "name": "self_hosted.production",
    "target": "self_hosted",
    "data_plane": {"object_store": "minio", "queue": "redis"},
}
UPSTREAM = {
    "name": "omi_cloud.production",
    "target": "omi_cloud",
    "data_plane": {"object_store": "gcs", "queue": "cloud_tasks"},
}


class PatchRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module_name = f"fork_test_target_{self.id().rsplit('.', 1)[-1]}"
        self.module = _module(self.module_name, make_client=lambda: "upstream-client")
        self.addCleanup(sys.modules.pop, self.module_name, None)

    def patch(self, **overrides) -> Patch:
        defaults = dict(
            name="test.client",
            module=self.module_name,
            attribute="make_client",
            build=lambda original: (lambda: "fork-client"),
            applies_to=lambda profile: profile["data_plane"]["object_store"] == "minio",
            reason="test",
        )
        defaults.update(overrides)
        return Patch(**defaults)

    def test_upstream_mode_applies_nothing(self):
        registry = build_registry([self.patch()]).apply(UPSTREAM)
        self.assertEqual(registry.applied, [])
        self.assertEqual(self.module.make_client(), "upstream-client")

    def test_matching_profile_swaps_the_symbol(self):
        registry = build_registry([self.patch()]).apply(SELF_HOSTED)
        self.assertEqual(registry.applied, ["test.client"])
        self.assertEqual(self.module.make_client(), "fork-client")

    def test_missing_target_fails_loudly_and_names_the_symbol(self):
        # This is the upstream-rename scenario: the patch must not silently
        # no-op, leaving a self-hosted process talking to a cloud it cannot reach.
        registry = build_registry([self.patch(attribute="gone_upstream")])
        with self.assertRaises(PatchError) as caught:
            registry.apply(SELF_HOSTED)
        message = str(caught.exception)
        self.assertIn("gone_upstream", message)
        self.assertIn("do not reintroduce a fork edit", message)

    def test_missing_module_fails_loudly(self):
        registry = build_registry([self.patch(module="fork_test_module_that_moved")])
        with self.assertRaises(PatchError) as caught:
            registry.apply(SELF_HOSTED)
        self.assertIn("cannot import", str(caught.exception))

    def test_a_broken_patch_leaves_earlier_ones_unapplied(self):
        # verify() runs before any swap, so the process is never half-patched.
        other = _module(f"{self.module_name}_two", make_client=lambda: "upstream-two")
        self.addCleanup(sys.modules.pop, f"{self.module_name}_two", None)
        registry = build_registry(
            [
                self.patch(name="good"),
                self.patch(name="broken", module=f"{self.module_name}_two", attribute="absent"),
            ]
        )
        with self.assertRaises(PatchError):
            registry.apply(SELF_HOSTED)
        self.assertEqual(self.module.make_client(), "upstream-client")
        self.assertEqual(other.make_client(), "upstream-two")

    def test_verify_does_not_mutate(self):
        registry = build_registry([self.patch()])
        registry.verify(SELF_HOSTED)
        self.assertEqual(self.module.make_client(), "upstream-client")
        self.assertEqual(registry.applied, [])

    def test_build_receives_the_original_so_a_patch_can_wrap(self):
        def wrap(original):
            return lambda: f"wrapped({original()})"

        build_registry([self.patch(build=wrap)]).apply(SELF_HOSTED)
        self.assertEqual(self.module.make_client(), "wrapped(upstream-client)")

    def test_duplicate_patch_names_are_rejected(self):
        with self.assertRaises(PatchError):
            build_registry([self.patch(), self.patch()])


if __name__ == "__main__":
    unittest.main(verbosity=2)
