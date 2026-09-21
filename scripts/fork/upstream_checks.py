#!/usr/bin/env python3
"""Execute the upstream manifest with fork-aware diff hygiene only.

Selection, metadata flags, platform filtering and execution stay with the
upstream runner. A temporary manifest replaces one command, never its triggers
or any other check. The tracked upstream manifest and checker remain unchanged.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
from run_checks import load_manifest  # noqa: E402


def effective_manifest() -> str:
    manifest = asdict(load_manifest(ROOT / '.github/checks-manifest.yaml'))
    matches = [check for check in manifest['checks'] if check['id'] == 'diff-hygiene']
    if len(matches) != 1:
        raise ValueError('upstream diff-hygiene check missing or duplicated; review its new owner')
    command = list(matches[0]['command'])
    original = '.github/scripts/check_diff_hygiene.py'
    if command.count(original) != 1:
        raise ValueError('upstream diff-hygiene command changed; review the fork adaptation')
    command[command.index(original)] = 'scripts/fork/check_diff_hygiene.py'
    matches[0]['command'] = command
    lines = []
    for section, entries in manifest.items():
        lines.append(f'{section}:')
        for entry in entries:
            for index, (key, value) in enumerate(entry.items()):
                prefix = '  - ' if index == 0 else '    '
                lines.append(f'{prefix}{key}: {json.dumps(value)}')
    return '\n'.join(lines) + '\n'


def main(arguments: list[str]) -> int:
    if any(argument == '--manifest' or argument.startswith('--manifest=') for argument in arguments):
        print('FAIL: upstream_checks owns its manifest; use the shared runner for other manifests.', file=sys.stderr)
        return 2
    try:
        content = effective_manifest()
    except (OSError, ValueError) as exc:
        print(f'FAIL: cannot adapt upstream checks: {exc}', file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix='omi-upstream-checks-') as directory:
        path = Path(directory) / 'manifest.yaml'
        path.write_text(content, encoding='utf-8')
        return subprocess.call(
            [sys.executable, str(ROOT / '.github/scripts/run_checks.py'), '--manifest', str(path), *arguments],
            cwd=ROOT,
        )


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
