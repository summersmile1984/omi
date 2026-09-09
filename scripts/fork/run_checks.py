#!/usr/bin/env python3
"""Select the fork's complete manifest for release preparation, or its diff for CI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def command(arguments: list[str], full: bool) -> list[str]:
    # Reuse the upstream manifest parser and execution owner; no copied checks.
    sys.path.insert(0, str(ROOT / '.github/scripts'))
    from run_checks import _platform_matches, detect_platform, load_manifest

    manifest = ROOT / '.github/checks-manifest.fork.yaml'
    result = [sys.executable, str(ROOT / '.github/scripts/run_checks.py'), '--manifest', str(manifest), *arguments]
    if full:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument('--platform', default=detect_platform())
        parser.add_argument('--exclusive-platform', action='store_true')
        parser.add_argument('--lane', required=True)
        options, _ = parser.parse_known_args(arguments)
        for check in load_manifest(manifest).checks:
            if options.lane in check.lanes and _platform_matches(
                check, options.platform, exclusive=options.exclusive_platform
            ):
                result.extend(['--check-id', check.id])
    return result


if __name__ == '__main__':
    raise SystemExit(subprocess.call(command(sys.argv[1:], os.environ.get('FORK_FULL_CHECKS') == 'true'), cwd=ROOT))
