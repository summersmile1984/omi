"""Run the upstream ``backend/testing/e2e/`` hermetic suite in a clean env.

The e2e harness (``backend/testing/e2e/conftest.py``) sets its own
environment variables at module import time and then imports the
real FastAPI backend. If the parent shell leaks variables that
override the harness's expectations — for example a stale
``GOOGLE_APPLICATION_CREDENTIALS`` left behind by a previous
``make dev-up`` cycle, or a developer's custom ``FIRESTORE_EMULATOR_HOST``
pointing at a dead port — the hermetic ``fake-firestore`` and
``google.auth.default`` monkeypatches get bypassed and the backend
calls the real Google APIs. The test then surfaces as a
``PermissionDenied: 403 Cloud Firestore API has not been used`` 503,
which is misleading because the suite itself is fine.

The right contract: the e2e suite wants a **clean** environment with
exactly the variables the harness sets. This script is that
contract — it builds a fresh ``os.environ`` containing only the
harness's own e2e env block (plus a minimal POSIX baseline so the
interpreter and pytest can start) and execs ``pytest testing/e2e/``
under it.

The script is intentionally a thin wrapper around ``subprocess.run``:
any future change in the e2e harness's required environment can be
made by editing ``_E2E_ENV`` here without touching the upstream
conftest.

Run directly:

    python scripts/dev-harness/dev_harness/run_e2e.py
    python scripts/dev-harness/dev_harness/run_e2e.py testing/e2e/test_crud.py
    python scripts/dev-harness/dev_harness/run_e2e.py -k test_seed_and_read_conversation

Exit code propagates the pytest return value so ``make dev-check``
can wire this in directly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"

# Mirror of the upstream ``backend/testing/e2e/conftest.py::_set_e2e_env``.
# The two MUST stay in sync; if conftest changes its e2e env block, copy
# it here too. Kept as a literal dict (not an import) so this script does
# not need a working Python venv to render the values.
_E2E_ENV: dict[str, str] = {
    "PYTHON_DOTENV_DISABLED": "1",
    "LOCAL_DEVELOPMENT": "true",
    "ENCRYPTION_SECRET": "test-encryption-secret-for-e2e-testing-32chars!",
    "FIREBASE_PROJECT_ID": "test-e2e-project",
    "GOOGLE_CLOUD_PROJECT": "test-e2e-project",
    "REDIS_DB_HOST": "localhost",
    "REDIS_DB_PORT": "6379",
    "REDIS_DB_PASSWORD": "",
    "DEEPGRAM_API_KEY": "fake-deepgram-key",
    "OPENAI_API_KEY": "fake-openai-key",
    "ANTHROPIC_API_KEY": "fake-anthropic-key",
    "OPENROUTER_API_KEY": "fake-openrouter-key",
    "GOOGLE_API_KEY": "fake-google-key",
    "TYPESENSE_HOST": "localhost",
    "TYPESENSE_HOST_PORT": "8108",
    "TYPESENSE_API_KEY": "fake-typesense-key",
    "TYPESENSE_CONVERSATION_INDEX_WRITES": "0",
    "BUCKET_SPEECH_PROFILES": "speech-profiles",
    "BUCKET_POSTPROCESSING": "postprocessing",
    "BUCKET_PRIVATE_CLOUD_SYNC": "omi-private-cloud-sync",
    "BUCKET_TEMPORAL_SYNC_LOCAL": "sync-temporal",
    "BUCKET_MEMORIES_RECORDINGS": "memories-recordings",
    "BUCKET_APP_THUMBNAILS": "app-thumbnails",
    "BUCKET_CHAT_FILES": "chat-files",
    "BUCKET_DESKTOP_UPDATES": "desktop-updates",
    "DEV_WEBHOOK_RETRY_DELAYS": "0,0,0",
    "SYNC_DISPATCH_MODE": "inline",
    "AUDIO_MERGE_DISPATCH_MODE": "inline",
    "STRIPE_SECRET_KEY": "",
    "ADMIN_KEY": "",
    # The harness reads MEMORY_MODE inside ``_set_e2e_env``; without this
    # explicit override, ``_create_backend_app`` raises on universal-memory
    # rollout assertions.
    "MEMORY_MODE": "read",
    "E2E_PYTEST_TIMEOUT": "0",
}

# Minimal POSIX baseline so the interpreter can start and pytest can
# write its cache. These are the only ``os.environ`` entries the runner
# preserves from the parent shell.
_PRESERVE_KEYS = ("TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHLVL")


def _build_clean_env() -> dict[str, str]:
    env = dict(_E2E_ENV)
    for key in _PRESERVE_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    # Allow operator overrides for the venv path on systems where
    # ``backend/.venv/bin/python`` is not on PATH (macOS cron, fresh
    # container, etc.).
    venv = BACKEND_DIR / ".venv" / "bin"
    if venv.is_dir() and "PATH" not in env:
        env["PATH"] = f"{venv}:/usr/local/bin:/usr/bin:/bin"
    return env


def main(argv: list[str]) -> int:
    pytest_args = argv if argv else ["testing/e2e/", "-q", "--tb=line", "-p", "no:cacheprovider"]
    if "--rootdir" not in pytest_args and "-c" not in pytest_args:
        # Pin the rootdir to the backend so pytest picks up ``pyproject.toml``
        # (which configures asyncio_mode) instead of walking up to the
        # repo root and finding the workspace-level config.
        pytest_args = ["--rootdir", str(BACKEND_DIR), *pytest_args]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *pytest_args],
        cwd=str(BACKEND_DIR),
        env=_build_clean_env(),
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))