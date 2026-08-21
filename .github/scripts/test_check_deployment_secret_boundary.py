#!/usr/bin/env python3
"""Unit fixtures for the name-only deployment setting boundary ratchet."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = REPO_ROOT / ".github" / "scripts" / "check_deployment_secret_boundary.py"
SPEC = importlib.util.spec_from_file_location("deployment_secret_boundary", CHECKER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {CHECKER_PATH}")
CHECKER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKER
SPEC.loader.exec_module(CHECKER)

POLICY = {
    "kinds": {
        "secret": ["FAKE_SERVER_SECRET"],
        "config": ["FAKE_RUNTIME_CONFIG"],
        "public_build": ["FAKE_PUBLIC_BUILD"],
    },
    "exceptions": {},
}


def git_environment() -> dict[str, str]:
    """Temporary fixture repositories must not inherit the hook's Git paths."""
    return {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}


class DeploymentSecretBoundaryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="omi-deployment-boundary-")
        self.root = Path(self.temp_dir.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, env=git_environment())
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"], cwd=self.root, check=True, env=git_environment()
        )
        subprocess.run(
            ["git", "config", "user.name", "Boundary Test"], cwd=self.root, check=True, env=git_environment()
        )
        self.write("config/deployment-setting-classification.json", json.dumps(POLICY))
        self.write(".github/workflows/deploy.yml", "name: test\n# utf-8 guard: \u2603\n")
        self.commit("baseline")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, relative_path: str, contents: str) -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def commit(self, message: str) -> None:
        subprocess.run(["git", "add", "."], cwd=self.root, check=True, env=git_environment())
        subprocess.run(["git", "commit", "-qm", message], cwd=self.root, check=True, env=git_environment())

    def errors(self) -> list[str]:
        policy = CHECKER.load_policy(self.root / "config/deployment-setting-classification.json")
        return CHECKER.validate_policy(policy) + CHECKER.validate_bindings(
            policy,
            CHECKER.extract_current_bindings(self.root),
            CHECKER.extract_base_bindings(self.root, "HEAD"),
        )

    def test_accepts_correct_secret_and_variable_bindings(self) -> None:
        self.write(
            ".github/workflows/deploy.yml",
            """name: deploy
jobs:
  deploy:
    env:
      CONFIG: ${{ vars.FAKE_RUNTIME_CONFIG }}
      TOKEN: ${{ secrets.FAKE_SERVER_SECRET }}
      BUILD: ${{ vars.FAKE_PUBLIC_BUILD }}
""",
        )

        self.assertEqual(self.errors(), [])

    def test_rejects_public_build_setting_from_github_secret(self) -> None:
        self.write(".github/workflows/deploy.yml", "BUILD: ${{ secrets.FAKE_PUBLIC_BUILD }}\n")

        self.assertIn(
            "public_build setting FAKE_PUBLIC_BUILD must use vars.FAKE_PUBLIC_BUILD", "\n".join(self.errors())
        )

    def test_rejects_config_external_secret_mapping(self) -> None:
        self.write(
            "backend/charts/backend-secrets/dev_omi_backend_secrets_values.yaml",
            """externalSecret:
  secretKeys:
    - secretKey: FAKE_RUNTIME_CONFIG
      remoteKey: FAKE_RUNTIME_CONFIG
""",
        )

        self.assertIn(
            "external_secret binding FAKE_RUNTIME_CONFIG is config; expected secret", "\n".join(self.errors())
        )

    def test_rejects_secret_from_github_variable(self) -> None:
        self.write(".github/workflows/deploy.yml", "TOKEN: ${{ vars.FAKE_SERVER_SECRET }}\n")

        self.assertIn(
            "github_vars binding FAKE_SERVER_SECRET is secret; expected config or public_build",
            "\n".join(self.errors()),
        )

    def test_rejects_config_loaded_from_secret_manager(self) -> None:
        self.write(
            ".github/workflows/deploy.yml",
            'echo "FAKE_RUNTIME_CONFIG=$(gcloud secrets versions access latest --secret=FAKE_SERVER_SECRET)"\n',
        )

        self.assertIn("secret_manager binding FAKE_RUNTIME_CONFIG is config; expected secret", "\n".join(self.errors()))

    def test_rejects_new_unclassified_binding_but_allows_legacy_baseline(self) -> None:
        self.write(".github/workflows/deploy.yml", "TOKEN: ${{ secrets.FAKE_LEGACY_NAME }}\n")
        self.commit("legacy binding")
        self.assertEqual(self.errors(), [])

        self.write(
            ".github/workflows/other.yml",
            "TOKEN: ${{ secrets.FAKE_LEGACY_NAME }}\n",
        )
        self.assertIn("github_secrets binding FAKE_LEGACY_NAME is unclassified", "\n".join(self.errors()))

    def test_rejects_malformed_exception_metadata(self) -> None:
        policy = dict(POLICY)
        policy["exceptions"] = {"FAKE_RUNTIME_CONFIG": {"owner": "platform"}}
        self.write("config/deployment-setting-classification.json", json.dumps(policy))

        errors = CHECKER.validate_policy(
            CHECKER.load_policy(self.root / "config/deployment-setting-classification.json")
        )

        self.assertIn("exception FAKE_RUNTIME_CONFIG is missing reason", errors)
        self.assertIn("exception FAKE_RUNTIME_CONFIG is missing expires", errors)
        self.assertIn("exception FAKE_RUNTIME_CONFIG is missing allowed_sources", errors)

    def test_current_tree_paths_use_git_posix_separators(self) -> None:
        windows_path = PureWindowsPath(r"C:\repo\.github\workflows\deploy.yml")
        windows_root = PureWindowsPath(r"C:\repo")
        posix_path = PurePosixPath("/repo/.github/workflows/deploy.yml")
        posix_root = PurePosixPath("/repo")

        self.assertEqual(CHECKER.repository_relative_path(windows_path, windows_root), ".github/workflows/deploy.yml")
        self.assertEqual(
            CHECKER.repository_relative_path(windows_path, windows_root),
            CHECKER.repository_relative_path(posix_path, posix_root),
        )

    @mock.patch.object(CHECKER.subprocess, "run")
    def test_base_git_reads_decode_utf8_explicitly(self, run: mock.Mock) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout=".github/workflows/deploy.yml\n"),
            subprocess.CompletedProcess([], 0, stdout="# utf-8 guard: \u2603\n"),
        ]

        self.assertEqual(CHECKER._base_paths(self.root, "HEAD"), {".github/workflows/deploy.yml"})
        self.assertEqual(
            CHECKER._read_base_file(self.root, "HEAD", ".github/workflows/deploy.yml"),
            "# utf-8 guard: \u2603\n",
        )
        self.assertEqual(len(run.call_args_list), 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["encoding"], "utf-8")


class RepositoryDeploymentSettingPolicyTest(unittest.TestCase):
    def test_speaker_embedding_deploy_controls_are_runtime_config(self) -> None:
        policy = CHECKER.load_policy(REPO_ROOT / "config/deployment-setting-classification.json")
        config_names = CHECKER._policy_kinds(policy)["config"]

        self.assertIn("SPEAKER_EMBEDDING_API_URL", config_names)
        self.assertIn("SPEAKER_EMBEDDING_PROVIDER", config_names)

    def test_self_host_capability_bindings_have_explicit_secret_or_config_ownership(self) -> None:
        policy = CHECKER.load_policy(REPO_ROOT / "config/deployment-setting-classification.json")
        kinds = CHECKER._policy_kinds(policy)

        expected_secrets = {
            "FIRMWARE_RELEASE_MANIFEST_BEARER_TOKEN",
            "IMAGE_GENERATION_OPENAI_COMPATIBLE_API_KEY",
            "MLX_MOSS_DIARIZE_API_KEY",
            "REALTIME_RELAY_API_KEY",
            "TTS_OPENAI_COMPATIBLE_API_KEY",
            "TYPESENSE_API_KEY",
        }
        expected_config = {
            "APP_ICON_GENERATION_TRANSPORT",
            "FILE_CHAT_LOCAL_MAX_CONTEXT_CHARACTERS",
            "FILE_CHAT_LOCAL_MAX_FILE_BYTES",
            "FILE_CHAT_LOCAL_MAX_IMAGE_PIXELS",
            "FILE_CHAT_LOCAL_MAX_INLINE_IMAGE_BYTES",
            "FILE_CHAT_LOCAL_MAX_TOTAL_BYTES",
            "FILE_CHAT_LOCAL_MAX_TOTAL_INLINE_IMAGE_BYTES",
            "FILE_CHAT_TRANSPORT",
            "FIRMWARE_RELEASE_ASSET_ORIGIN",
            "FIRMWARE_RELEASE_MANIFEST_URL",
            "FIRMWARE_RELEASE_TRANSPORT",
            "IMAGE_GENERATION_OPENAI_COMPATIBLE_BASE_URL",
            "IMAGE_GENERATION_OPENAI_COMPATIBLE_MODEL",
            "MEMORY_KEYWORD_INDEX_PROVIDER",
            "MEMORY_TYPESENSE_COLLECTION",
            "MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH",
            "MLX_MOSS_DIARIZE_ENDPOINT",
            "MLX_MOSS_DIARIZE_MODEL",
            "PUSH_PROVIDER",
            "REALTIME_RELAY_ALLOWED_HOSTS",
            "REALTIME_RELAY_MAX_MESSAGE_BYTES",
            "REALTIME_RELAY_MAX_SESSION_SECONDS",
            "REALTIME_RELAY_PROVIDER_ID",
            "REALTIME_RELAY_URL",
            "REALTIME_RELAY_WIRE_PROTOCOL",
            "SPEAKER_EMBEDDING_MODEL",
            "SPEAKER_EMBEDDING_NUM_THREADS",
            "SPEAKER_MODEL_HOST_DIR",
            "TTS_MODEL_HOST_DIR",
            "TTS_OPENAI_COMPATIBLE_BASE_URL",
            "TTS_OPENAI_COMPATIBLE_MODEL",
            "TTS_OPENAI_COMPATIBLE_VOICE",
            "TTS_PROVIDER",
            "TTS_SHERPA_DATA_DIR",
            "TTS_SHERPA_MODEL",
            "TTS_SHERPA_NUM_THREADS",
            "TTS_SHERPA_SPEAKER_ID",
            "TTS_SHERPA_TOKENS",
            "TYPESENSE_HOST",
            "TYPESENSE_HOST_PORT",
            "TYPESENSE_PROTOCOL",
        }

        self.assertTrue(expected_secrets.issubset(kinds["secret"]))
        self.assertTrue(expected_config.issubset(kinds["config"]))
        self.assertFalse(expected_secrets & kinds["config"])
        self.assertFalse(expected_config & kinds["secret"])


if __name__ == "__main__":
    unittest.main()
