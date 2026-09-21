#!/usr/bin/env python3
"""Process ownership regressions for the shared fork local lifecycle.

Real disposable processes prove stop cannot claim a listener by name or port,
reuse an unrelated PID, or interpret an invalid PID as a process-group signal.
No Docker, backend dependencies, or provider credentials are required.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SELFHOST_SH = REPO_ROOT / "dev" / "selfhost-local.sh"


class SelfhostLocalShTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="omi-selfhost-selftest-")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.env = {
            key: value for key, value in os.environ.items() if not key.startswith(("OMI_LOCAL_", "OMI_SELFHOST_"))
        }
        self.env.update(
            OMI_LOCAL_STATE_DIR=str(self.state),
            OMI_LOCAL_ENV_FILE=str(REPO_ROOT / "dev/local.env.example"),
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.env["OMI_LOCAL_BACKEND_PORT"] = str(probe.getsockname()[1])

    def run_selfhost(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SELFHOST_SH), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=75,
        )

    def sleeper(self) -> subprocess.Popen:
        process = subprocess.Popen(["sleep", "120"])

        def cleanup() -> None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)

        self.addCleanup(cleanup)
        return process

    def record_pid(self, process: subprocess.Popen, *, valid_identity: bool = True) -> None:
        pids = self.state / "pids"
        pids.mkdir(exist_ok=True)
        (pids / "backend.pid").write_text(f"{process.pid}\n")
        identity = subprocess.check_output(["ps", "-o", "lstart=", "-o", "command=", "-p", str(process.pid)], text=True)
        (pids / "backend.pid.started").write_text(identity if valid_identity else "previous process identity\n")

    def test_stop_leaves_unrecorded_process_alive(self) -> None:
        process = self.sleeper()
        result = self.run_selfhost("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(process.poll())

    def test_stop_rejects_reused_pid(self) -> None:
        process = self.sleeper()
        self.record_pid(process, valid_identity=False)
        result = self.run_selfhost("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(process.poll(), "a stale PID must not terminate its new owner")

    def test_stop_terminates_recorded_process(self) -> None:
        process = self.sleeper()
        self.record_pid(process)
        result = self.run_selfhost("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(process.wait(timeout=10), 0)
        self.assertFalse((self.state / "pids/backend.pid").exists())

    def test_invalid_pid_cannot_signal_a_process_group(self) -> None:
        process = self.sleeper()
        pids = self.state / "pids"
        pids.mkdir()
        (pids / "backend.pid").write_text("-1\n")
        (pids / "backend.pid.started").write_text("invalid\n")
        result = self.run_selfhost("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(process.poll())

    def test_other_instance_cannot_stop_owned_process(self) -> None:
        process = self.sleeper()
        self.record_pid(process)
        with tempfile.TemporaryDirectory() as other:
            self.env["OMI_LOCAL_STATE_DIR"] = other
            result = self.run_selfhost("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(process.poll())

    def test_unhealthy_owned_process_is_not_reported_ready(self) -> None:
        # No socket is served by sleep. PID liveness alone cannot satisfy up.
        process = self.sleeper()
        self.record_pid(process)
        for suffix in (".pid", ".pid.started"):
            (self.state / f"pids/queue-worker{suffix}").write_bytes((self.state / f"pids/backend{suffix}").read_bytes())
        (self.state / "ai-profile").write_text("core-only\n")
        result = self.run_selfhost("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(process.poll())


if __name__ == "__main__":
    unittest.main()
