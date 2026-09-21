#!/usr/bin/env python3
"""Hermetic tests for dev/local.sh — the stage-1 local dev entry point.

These run without docker, without the stack, and without a network: they cover
the parts of the script that decide *what* would be started (configuration
layering, port collision reporting, command surface). Starting the real stack is
what `dev/local.sh up && dev/local.sh verify` proves, not this file.

Run: python3 dev/tests/test_local_sh.py   (or: make -f Makefile.fork local-selftest)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_SH = REPO_ROOT / "dev" / "local.sh"


def run_local(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    merged = {key: value for key, value in os.environ.items() if not key.startswith("OMI_LOCAL_")}
    # Keep every run inside its own state dir so the tests never touch a real stack.
    merged["OMI_LOCAL_STATE_DIR"] = tempfile.mkdtemp(prefix="omi-local-selftest-")
    merged["OMI_LOCAL_ENV_FILE"] = str(REPO_ROOT / "dev" / "local.env.example")
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(LOCAL_SH), *arguments],
        cwd=REPO_ROOT,
        env=merged,
        capture_output=True,
        text=True,
        timeout=120,
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# The example defaults (5434 / 9000 / 9001) may be taken by unrelated software on
# a developer machine, and these tests must not depend on that. Every port is
# pinned to a free one unless the test is specifically about a collision.
PORT_KEYS = (
    "OMI_LOCAL_POSTGRES_PORT",
    "OMI_LOCAL_REDIS_PORT",
    "OMI_LOCAL_MINIO_API_PORT",
    "OMI_LOCAL_MINIO_CONSOLE_PORT",
    "OMI_LOCAL_FIRESTORE_PORT",
    "OMI_LOCAL_FIREBASE_AUTH_PORT",
    "OMI_LOCAL_FIREBASE_STORAGE_PORT",
    "OMI_LOCAL_AUTH_PORT",
    "OMI_LOCAL_BACKEND_PORT",
    "OMI_LOCAL_QDRANT_PORT",
    "OMI_LOCAL_QDRANT_GRPC_PORT",
    "OMI_LOCAL_TYPESENSE_PORT",
)


def free_ports() -> dict[str, str]:
    return {key: str(free_port()) for key in PORT_KEYS}


class LocalShTests(unittest.TestCase):
    def test_unknown_command_fails_loudly(self) -> None:
        result = run_local("definitely-not-a-command")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown command", result.stderr)

    def test_local_env_overrides_the_example_defaults(self) -> None:
        backend_port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "local.env"
            override.write_text(f"OMI_LOCAL_BACKEND_PORT={backend_port}\n", encoding="utf-8")
            env = free_ports()
            env.update({"OMI_LOCAL_ENV_FILE": str(override), "OMI_LOCAL_BACKEND_PORT": str(backend_port)})
            result = run_local("ports", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(backend_port), result.stdout)
            self.assertIn(str(override), result.stdout)

    def test_ambient_environment_beats_both_files(self) -> None:
        ambient_port = free_port()
        file_port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "local.env"
            override.write_text(f"OMI_LOCAL_BACKEND_PORT={file_port}\n", encoding="utf-8")
            env = free_ports()
            env.update(
                {
                    "OMI_LOCAL_ENV_FILE": str(override),
                    "OMI_LOCAL_BACKEND_PORT": str(ambient_port),
                }
            )
            result = run_local("ports", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(ambient_port), result.stdout)
            self.assertNotIn(str(file_port), result.stdout)

    def test_a_port_held_by_another_process_is_reported_as_a_collision(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            # A generous backlog: the point of the test is a port that is in use,
            # not a socket that refuses extra probes.
            listener.listen(64)
            taken = int(listener.getsockname()[1])
            with tempfile.TemporaryDirectory() as tmp:
                override = Path(tmp) / "local.env"
                override.write_text(f"OMI_LOCAL_BACKEND_PORT={taken}\n", encoding="utf-8")
                env = free_ports()
                env.update({"OMI_LOCAL_ENV_FILE": str(override), "OMI_LOCAL_BACKEND_PORT": str(taken)})
                result = run_local("ports", env=env)
        self.assertEqual(result.returncode, 1, "a taken port must make `ports` exit non-zero")
        self.assertIn("backend", result.stderr)
        self.assertIn(str(taken), result.stderr)

    def test_env_prints_the_desktop_client_exports(self) -> None:
        result = run_local("env")
        self.assertEqual(result.returncode, 0, result.stderr)
        for key in (
            "export OMI_LOCAL_BACKEND_URL=",
            "export OMI_DESKTOP_API_URL=",
            "export FIREBASE_AUTH_EMULATOR_HOST=",
            "export AUTH_DEV_ISSUER_SECRET=",
        ):
            with self.subTest(key=key):
                self.assertIn(key, result.stdout)

    def test_configuration_error_when_a_required_value_is_blank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "local.env"
            override.write_text("OMI_LOCAL_BACKEND_PORT=\n", encoding="utf-8")
            result = run_local("ports", env={"OMI_LOCAL_ENV_FILE": str(override)})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("OMI_LOCAL_BACKEND_PORT is empty", result.stderr)

    def child_environment(self, profile: str | None, extra: dict[str, str]) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as state:
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    '. "$1" help >/dev/null; select_ai; write_child_env; run_backend /usr/bin/env',
                    "local-env-test",
                    str(LOCAL_SH),
                ],
                env={
                    **{key: value for key, value in os.environ.items() if not key.startswith("OMI_LOCAL_")},
                    "OMI_LOCAL_STATE_DIR": state,
                    "OMI_LOCAL_ENV_FILE": str(REPO_ROOT / "dev" / "local.env.example"),
                    "OMI_LOCAL_SPEECH_MODEL_STORE": state,
                    **({"OMI_LOCAL_AI_PROFILE": profile} if profile is not None else {}),
                    **extra,
                },
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_native_child_does_not_inherit_provider_or_cloud_authority(self) -> None:
        child = self.child_environment(
            None,
            {
                "OPENAI_API_KEY": "ambient-openai",
                "MIMO_API_KEY": "ambient-mimo",
                "GOOGLE_APPLICATION_CREDENTIALS": "/ambient/credentials",
                "OMI_LOCAL_OPENROUTER_API_KEY": "unselected-local",
                "OMI_LOCAL_MIMO_API_KEY": "unselected-mimo",
                "OMI_LOCAL_LLM_ENDPOINT": "http://127.0.0.1:11435",
                "OMI_LOCAL_EMBEDDING_ENDPOINT": "http://127.0.0.1:11436",
            },
        )
        for key in ("OPENAI_API_KEY", "MIMO_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "OPENROUTER_API_KEY"):
            self.assertNotIn(key, child)
        self.assertEqual(child["OMI_DEPLOYMENT_TARGET"], "self_hosted")
        self.assertEqual(child["AUTH_PROVIDER"], "better_auth")
        self.assertIn("OMI_HARNESS_INSTANCE", child)  # Disables backend dotenv loading.
        self.assertEqual(child["LLM_ENDPOINT"], "http://127.0.0.1:11435")
        self.assertEqual(child["EMBEDDING_ENDPOINT"], "http://127.0.0.1:11436")
        self.assertTrue(Path(child["SPEECH_MODEL_STORE"]).is_absolute())

    def test_local_lifecycle_renders_full_native_profile_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    '. "$1" help >/dev/null; PYTHON_BIN="$2"; select_ai; render_profile',
                    "local-render-test",
                    str(LOCAL_SH),
                    sys.executable,
                ],
                env={
                    **{key: value for key, value in os.environ.items() if not key.startswith("OMI_LOCAL_")},
                    "OMI_LOCAL_STATE_DIR": state,
                    "OMI_LOCAL_ENV_FILE": str(REPO_ROOT / "dev/local.env.example"),
                },
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            row = json.loads((Path(state) / "deployment_profiles.generated.json").read_text())["profiles"][
                "self_hosted.local"
            ]
        self.assertEqual(row["capabilities"]["llm_provider"], "ollama")
        self.assertEqual(row["capabilities"]["stt_providers"], ["sensevoice"])
        self.assertEqual(row["capabilities"]["tts_provider"], "kokoro")
        self.assertEqual(row["embedding"]["model"], "bge-m3:latest")

    def test_retired_selectors_fail_before_starting_services(self) -> None:
        for args, config in (
            (("up", "--core-only"), {}),
            (("restart", "--core-only"), {}),
            (("up",), {"OMI_LOCAL_AI_PROFILE": "core-only"}),
            (("up", "--operator-ai", "mimo-cn"), {"OMI_LOCAL_AI_PROFILE": "core-only"}),
        ):
            with self.subTest(args=args, config=config):
                result = run_local(*args, env=config)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid local AI profile" if config else "unknown option", result.stderr)

    def test_native_requires_explicit_speech_store_without_ambient_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            result = subprocess.run(
                ["bash", "-c", '. "$1" help >/dev/null; select_ai; write_child_env', "local-env-test", str(LOCAL_SH)],
                env={
                    **{key: value for key, value in os.environ.items() if not key.startswith("OMI_LOCAL_")},
                    "OMI_LOCAL_STATE_DIR": state,
                    "OMI_LOCAL_ENV_FILE": str(REPO_ROOT / "dev/local.env.example"),
                    "SPEECH_MODEL_STORE": state,
                    "OMI_LOCAL_SPEECH_MODEL_STORE": "",
                },
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(state) / "child.env").exists())
            self.assertIn("OMI_LOCAL_SPEECH_MODEL_STORE", result.stderr)

    def test_operator_ai_child_receives_only_scoped_selected_credential(self) -> None:
        child = self.child_environment(
            "openrouter",
            {
                "OPENROUTER_API_KEY": "ambient-wrong",
                "OMI_LOCAL_OPENROUTER_API_KEY": "explicit-local",
                "OMI_LOCAL_MIMO_API_KEY": "unselected-local",
                "MIMO_API_KEY": "ambient-wrong",
            },
        )
        self.assertEqual(child["OPENROUTER_API_KEY"], "explicit-local")
        self.assertNotIn("MIMO_API_KEY", child)
        self.assertEqual(child["OMI_DEPLOYMENT_PROFILE"], "self_hosted.local")
        self.assertTrue(child["FIRESTORE_PG_DSN"].startswith("postgresql+psycopg://"))

    def test_mimo_uses_scoped_key_and_real_embedding_endpoint(self) -> None:
        child = self.child_environment(
            "mimo-cn",
            {
                "MIMO_API_KEY": "ambient-wrong",
                "OMI_LOCAL_MIMO_API_KEY": "explicit-local",
                "OMI_LOCAL_EMBEDDING_ENDPOINT": "http://127.0.0.1:11436",
                "OMI_LOCAL_SPEECH_MODEL_STORE": "",
            },
        )
        self.assertEqual(child["MIMO_API_KEY"], "explicit-local")
        self.assertEqual(child["EMBEDDING_ENDPOINT"], "http://127.0.0.1:11436")
        self.assertNotIn("LLM_ENDPOINT", child)
        self.assertNotIn("SPEECH_MODEL_STORE", child)

    def test_ambient_provider_key_cannot_authorize_operator_ai(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            result = subprocess.run(
                ["bash", "-c", '. "$1" help >/dev/null; select_ai; write_child_env', "local-env-test", str(LOCAL_SH)],
                env={
                    **os.environ,
                    "OMI_LOCAL_STATE_DIR": state,
                    "OMI_LOCAL_ENV_FILE": str(REPO_ROOT / "dev" / "local.env.example"),
                    "OMI_LOCAL_AI_PROFILE": "openrouter",
                    "OMI_LOCAL_OPENROUTER_API_KEY": "",
                    "OPENROUTER_API_KEY": "ambient-only",
                },
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertNotEqual(result.returncode, 0)

    def test_restart_retains_selection_and_namespace_unless_configuration_replaces_them(self) -> None:
        for command in ("cmd_restart", "cmd_backend_restart"):
            for source in ("retained", "ambient", "file", "profile-ambient", "profile-file"):
                with self.subTest(command=command, source=source), tempfile.TemporaryDirectory() as tmp:
                    state = Path(tmp)
                    (state / "ai-profile").write_text("openrouter\n")
                    (state / "qdrant-prefix").write_text("existing_hosted_vectors\n")
                    config = state / "local.env"
                    config.write_text(
                        "OMI_LOCAL_QDRANT_COLLECTION_PREFIX=explicit_vectors\n"
                        if source == "file"
                        else "OMI_LOCAL_AI_PROFILE=mimo-cn\n" if source == "profile-file" else ""
                    )
                    environment = {key: value for key, value in os.environ.items() if not key.startswith("OMI_LOCAL_")}
                    environment.update(
                        {
                            "OMI_LOCAL_STATE_DIR": tmp,
                            "OMI_LOCAL_ENV_FILE": str(config),
                            "OMI_LOCAL_OPENROUTER_API_KEY": "synthetic-key",
                            "OMI_LOCAL_MIMO_API_KEY": "synthetic-mimo-key",
                        }
                    )
                    if source == "ambient":
                        environment["OMI_LOCAL_QDRANT_COLLECTION_PREFIX"] = "explicit_vectors"
                    if source == "profile-ambient":
                        environment["OMI_LOCAL_AI_PROFILE"] = "mimo-cn"
                    result = subprocess.run(
                        [
                            "bash",
                            "-c",
                            '. "$1" help >/dev/null; '
                            'cmd_backend_stop() { :; }; stop_process() { :; }; '
                            'cmd_backend_up() { select_ai "$@"; write_child_env; run_backend /usr/bin/env; }; '
                            'cmd_up() { cmd_backend_up "$@"; }; "$2"',
                            "restart-contract",
                            str(LOCAL_SH),
                            command,
                        ],
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    child = dict(line.split("=", 1) for line in result.stdout.splitlines())
                    expected = "explicit_vectors" if source in ("ambient", "file") else "existing_hosted_vectors"
                    self.assertEqual(child["QDRANT_COLLECTION_PREFIX"], expected)
                    if source.startswith("profile-"):
                        self.assertIn("MIMO_API_KEY", child)
                        self.assertNotIn("OPENROUTER_API_KEY", child)
                    else:
                        self.assertIn("OPENROUTER_API_KEY", child)


if __name__ == "__main__":
    unittest.main(verbosity=2)
