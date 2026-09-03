"""Deployment-profile selection for backend processes."""

from __future__ import annotations

import unittest
from unittest import mock

from fork import profile


class ProfileSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        profile.reset()
        self.addCleanup(profile.reset)

    def resolve(self, **env):
        with mock.patch.dict("os.environ", env, clear=True):
            profile.reset()
            return profile.current()

    def test_default_is_upstream_behavior(self):
        # An unconfigured process must behave as upstream, not as a
        # half-configured fork deployment.
        row = self.resolve()
        self.assertEqual(row["name"], "omi_cloud.production")
        self.assertEqual(row["identity_provider"], "firebase")

    def test_explicit_profile_wins(self):
        row = self.resolve(OMI_DEPLOYMENT_PROFILE="omi_cloud.local")
        self.assertEqual(row["name"], "omi_cloud.local")

    def test_target_plus_upstream_stage_alias(self):
        # Upstream's stage vocabulary ("prod") maps onto the fork's ("production").
        row = self.resolve(OMI_DEPLOYMENT_TARGET="omi_cloud", OMI_ENV_STAGE="prod")
        self.assertEqual(row["name"], "omi_cloud.production")

    def test_unknown_profile_names_what_the_image_contains(self):
        with self.assertRaises(profile.ProfileError) as caught:
            self.resolve(OMI_DEPLOYMENT_PROFILE="self_hosted.production")
        message = str(caught.exception)
        self.assertIn("self_hosted.production", message)
        self.assertIn("wrong --target", message)

    def test_upstream_mode_helper(self):
        self.resolve()
        self.assertTrue(profile.is_upstream_mode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
