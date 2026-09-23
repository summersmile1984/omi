#!/usr/bin/env python3
"""Provision and run the fork's real Redis/Postgres qualification lane."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / 'backend'
VENV = ROOT / '.venv-fork-tests'
CONFIG = ROOT / 'dev' / 'pytest-containers.ini'


def run(command: list[str], env: dict[str, str]) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def docker_environment(env: dict[str, str]) -> dict[str, str]:
    if not shutil.which('docker'):
        raise RuntimeError('Docker is required: install Docker and start its daemon before running container tests.')
    # The Docker Python SDK does not read the active CLI context (notably Docker Desktop on macOS).
    if not env.get('DOCKER_HOST'):
        context = subprocess.run(
            ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        env['DOCKER_HOST'] = context.stdout.strip()
    result = subprocess.run(['docker', 'info'], env=env, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(
            'Docker daemon is unavailable. Start the configured Docker daemon and retry; tests are not skipped.'
        )
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--setup-only', action='store_true', help='sync .venv-fork-tests without starting Docker or tests'
    )
    args = parser.parse_args()
    env = os.environ.copy()
    try:
        if not shutil.which('uv'):
            raise RuntimeError(
                'uv is required; install it from https://docs.astral.sh/uv/getting-started/installation/.'
            )
        if not args.setup_only:
            env = docker_environment(env)
        if VENV.is_symlink():
            raise RuntimeError('.venv-fork-tests must be an isolated directory, not a symlink to another environment.')
        # Reuse upstream's exact interpreter and platform-lock selection without touching backend/.venv.
        run(['bash', str(BACKEND / 'scripts' / 'sync-python-deps.sh')], {**env, 'VENV_PATH': str(VENV)})
        python = VENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        run(
            [
                'uv',
                'pip',
                'install',
                '--python',
                str(python),
                '--require-hashes',
                '--no-deps',
                '-r',
                str(ROOT / 'dev' / 'requirements-test.txt'),
            ],
            env,
        )
        if args.setup_only:
            print(f'Fork container dependencies ready in {VENV}', flush=True)
            return 0
        env['PYTHONPATH'] = str(BACKEND)
        env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        env.pop('PYTEST_ADDOPTS', None)
        env.pop('PYTEST_PLUGINS', None)
        # Separate collection roots keep backend/tests/conftest.py and cloud credentials out of this lane.
        for directory, test in (
            (ROOT / 'dev' / 'tests' / 'containers', 'test_redis_container.py'),
            (BACKEND / 'firestore_pg' / 'tests', 'test_shadow_e2e.py'),
        ):
            run(
                [
                    str(python),
                    '-m',
                    'pytest',
                    '-q',
                    '-c',
                    str(CONFIG),
                    '--confcutdir',
                    str(directory),
                    str(directory / test),
                ],
                env,
            )
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'Fork container qualification failed: {exc}', file=sys.stderr)
        print(
            'Required: pinned Python/dependency downloads (or uv cache), a reachable Docker daemon, and Redis/Postgres images.',
            file=sys.stderr,
        )
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
