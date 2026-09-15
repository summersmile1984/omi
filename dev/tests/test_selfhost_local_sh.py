#!/usr/bin/env python3
"""Hermetic tests for dev/selfhost-local.sh — the stage-1 app-plane entry point.

No docker, no real backend, no network beyond a loopback socket. These cover what the
script decides: *what is running on the port* and *what stopping means*. Starting the
real backend is what `dev/local.sh verify` plus an authenticated call prove; a piped
`dev/selfhost-local.sh up` needs the data plane and is proven by the cold start instead.

Two regressions from the 2026-09-15 cold start are pinned here:

  * `stop` only knew the pid file, so a backend left running without one (an earlier stop,
    a wiped .local/, a crash) stayed up and kept :8100 taken while `stop` reported success.
  * `status` had the same blind spot, so it said "stopped" about a live backend.

Run: python3 dev/tests/test_selfhost_local_sh.py
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SELFHOST_SH = REPO_ROOT / "dev" / "selfhost-local.sh"
ENV_EXAMPLE = REPO_ROOT / "dev" / "selfhost-local.env.example"
TABLE = REPO_ROOT / "backend" / "fork" / "deployment_profiles.generated.json"

# The fake backend is `nc` launched under the backend's argv[0]. Not a Python listener:
# Xcode's python3 rewrites argv[0] on this machine, so `exec -a` would not survive and the
# script's name check could never match. nc is a plain binary and keeps the name.
FAKE_ARGV0 = "uvicorn fork.main:app"
# `nc -l` spelling differs between BSD (macOS) and netcat-openbsd (CI Linux); try them in
# order and keep whichever actually listens. -k keeps it listening after the liveness probe
# connects, and the caller keeps its stdin open because an EOF there also ends nc.
LISTEN_ARGV = (("-l", "-k", "127.0.0.1", "{port}"), ("-l", "-k", "-p", "{port}"), ("-l", "-k", "{port}"))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def run_selfhost(*arguments: str, state_dir: str, port: int) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Hermetic: its own state dir, the example env (never a developer's local.env), and a
    # free port that no real backend can be using.
    env["OMI_SELFHOST_STATE_DIR"] = state_dir
    env["OMI_SELFHOST_ENV_FILE"] = str(ENV_EXAMPLE)
    env["OMI_SELFHOST_PORT"] = str(port)
    return subprocess.run(
        ["bash", str(SELFHOST_SH), *arguments],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@contextmanager
def fake_backend(port: int, *, named: bool = True):
    """A listener on `port`; `named=False` for an unrelated process holding the port."""
    process = None
    for template in LISTEN_ARGV:
        argv = [part.format(port=port) for part in template]
        command = "exec " + (f"-a {shlex.quote(FAKE_ARGV0)} " if named else "") + "nc " + " ".join(argv)
        candidate = subprocess.Popen(
            ["bash", "-c", command],
            stdin=subprocess.PIPE,  # held open: an EOF on stdin ends nc
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if listening(port):
                process = candidate
                break
            if candidate.poll() is not None:
                break
            time.sleep(0.05)
        if process is not None:
            break
        candidate.kill()
        candidate.wait(timeout=10)
    if process is None:
        raise AssertionError(f"no `nc -l` spelling listened on {port}: {LISTEN_ARGV}")
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def port_listener(port: int) -> int | None:
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True
    )
    pid = result.stdout.strip().splitlines()
    return int(pid[0]) if pid else None


class SelfhostLocalShTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = tempfile.mkdtemp(prefix="omi-selfhost-selftest-")
        self.port = free_port()
        # The script renders `self_hosted.local` into its own state directory and points
        # the backend at it with OMI_DEPLOYMENT_PROFILES_PATH. It used to render the
        # tracked table in place and restore it from git, which left a local-stage render
        # behind whenever a run was killed -- the `fork-profile-tables` lane then failed
        # locally, and the render was one `git add -A` from being committed.
        self.table_bytes = TABLE.read_bytes() if TABLE.exists() else None

    def tearDown(self) -> None:
        if self.table_bytes is not None:
            self.assertEqual(
                TABLE.read_bytes(),
                self.table_bytes,
                "a local self-host run must not rewrite the tracked profile table",
            )

    def test_status_reports_stopped_when_nothing_listens(self) -> None:
        result = run_selfhost("status", state_dir=self.state_dir, port=self.port)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("backend: stopped", result.stdout)

    def test_status_finds_the_backend_when_the_pid_file_is_gone(self) -> None:
        with fake_backend(self.port) as listener:
            result = run_selfhost("status", state_dir=self.state_dir, port=self.port)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"backend: running (pid {listener.pid})", result.stdout)
            self.assertIn("no pid file", result.stdout)

    def test_stop_kills_the_backend_when_the_pid_file_is_gone(self) -> None:
        with fake_backend(self.port) as listener:
            result = run_selfhost("stop", state_dir=self.state_dir, port=self.port)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"backend stopped (pid {listener.pid})", result.stdout)
            listener.wait(timeout=10)
            self.assertIsNone(port_listener(self.port), "stop must release the port")

    def test_stop_leaves_an_unrelated_listener_alone(self) -> None:
        # The fallback may only ever claim this backend: an unrelated service that
        # happens to hold the configured port must survive `stop`. Behaviour only --
        # the message it prints is not the contract here.
        with fake_backend(self.port, named=False) as other:
            pid = port_listener(self.port)
            self.assertIsNotNone(pid, "the unrelated listener should hold the port")
            result = run_selfhost("stop", state_dir=self.state_dir, port=self.port)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(other.poll(), "stop killed a process that is not this backend")
            self.assertEqual(port_listener(self.port), pid, "the unrelated listener must keep serving")


if __name__ == "__main__":
    unittest.main()
