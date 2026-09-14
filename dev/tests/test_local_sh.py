#!/usr/bin/env python3
"""Hermetic tests for dev/local.sh — the stage-1 local dev entry point.

These run without docker, without the stack, and without a network: they cover
the parts of the script that decide *what* would be started (configuration
layering, port collision reporting, command surface). Starting the real stack is
what `dev/local.sh up && dev/local.sh verify` proves, not this file.

Run: python3 dev/tests/test_local_sh.py   (or: make -f Makefile.fork local-selftest)
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_SH = REPO_ROOT / "dev" / "local.sh"


def run_local(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    # Keep every run inside its own state dir so the tests never touch a real stack.
    merged["OMI_LOCAL_STATE_DIR"] = tempfile.mkdtemp(prefix="omi-local-selftest-")
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
)


def free_ports() -> dict[str, str]:
    return {key: str(free_port()) for key in PORT_KEYS}


class LocalShTests(unittest.TestCase):
    def test_help_lists_the_lifecycle_commands(self) -> None:
        result = run_local("help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("up", "status", "verify", "restart", "logs", "ports", "env", "down", "reset"):
            with self.subTest(command=command):
                self.assertIn(f"dev/local.sh {command}", result.stdout)

    def test_unknown_command_fails_loudly(self) -> None:
        result = run_local("definitely-not-a-command")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown command", result.stderr)

    def test_the_legacy_entry_point_forwards_to_this_script(self) -> None:
        # dev/deploy-local.sh must stay a forwarding shim; a read-only command
        # proves the wiring without executing anything that mutates the machine.
        # (`--stop` / `--no-backend` are deliberately NOT invoked here: they act
        # on the real containers and would tear down a developer's running stack.)
        wrapper = REPO_ROOT / "dev" / "deploy-local.sh"
        result = subprocess.run(
            ["bash", str(wrapper), "ports"],
            cwd=REPO_ROOT,
            env={**os.environ, "OMI_LOCAL_STATE_DIR": tempfile.mkdtemp(prefix="omi-local-selftest-")},
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertIn("dev/local.sh", result.stderr)
        self.assertIn("Omi local dev", result.stdout)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
