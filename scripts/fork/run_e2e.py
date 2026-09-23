#!/usr/bin/env python3
"""Run the upstream hermetic E2E suite without inheriting deployment credentials.

Usage: backend/.venv/bin/python scripts/fork/run_e2e.py [-k expression] [-q]

The upstream conftest owns its fake-service environment; this entry only clears
ambient deployment state and disables dotenv before pytest plugins can load.
The suite path is always present, even when the caller passes pytest options.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

BACKEND_DIR = Path(__file__).resolve().parents[2] / 'backend'
_PRESERVE_KEYS = ('TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TERM', 'SYSTEMROOT')


def clean_env() -> dict[str, str]:
    environment = {key: os.environ[key] for key in _PRESERVE_KEYS if key in os.environ}
    environment['PATH'] = f'{Path(sys.executable).parent}{os.pathsep}{os.defpath}'
    environment['PYTHON_DOTENV_DISABLED'] = '1'
    # Test-mode policy, not a duplicated provider/service configuration table.
    environment['MEMORY_MODE'] = 'read'
    environment['E2E_PYTEST_TIMEOUT'] = '0'
    return environment


def main(arguments: list[str]) -> int:
    return subprocess.call(
        [
            sys.executable,
            '-m',
            'pytest',
            '--rootdir',
            str(BACKEND_DIR),
            'testing/e2e/',
            '-p',
            'no:cacheprovider',
            *arguments,
        ],
        cwd=BACKEND_DIR,
        env=clean_env(),
    )


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
