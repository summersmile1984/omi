#!/usr/bin/env python3
"""Check fork-authored whitespace without rewriting incorporated upstream bytes.

Upstream-owned paths are compared with merge-base(head, upstream/main); fork
paths keep the event-base diff. Conflict markers are checked in every changed
text file, including unchanged upstream imports. No upstream checker is edited.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def git(*arguments: str) -> str:
    return subprocess.run(['git', *arguments], check=True, capture_output=True, text=True).stdout


def check(changed_files: list[str], base: str, head: str, upstream_ref: str) -> int:
    upstream = git('merge-base', head, upstream_ref).strip()
    event_base = git('merge-base', base, head).strip()
    upstream_paths = set(git('ls-tree', '-r', '--name-only', upstream).splitlines())
    upstream_changed = sorted(set(changed_files) & upstream_paths)
    fork_changed = sorted(set(changed_files) - upstream_paths)
    failed = False
    for paths, reference in ((upstream_changed, upstream), (fork_changed, event_base)):
        if not paths:
            continue
        # HEAD means the final checkout, including staged and unstaged changes.
        # Comparing directly to upstream also permits a restoration of its EOF.
        revisions = [reference] if head == 'HEAD' else [reference, head]
        result = subprocess.run(['git', 'diff', '--check', *revisions, '--', *paths], check=False)
        failed |= result.returncode != 0

    untracked = set(git('ls-files', '--others', '--exclude-standard').splitlines()) if head == 'HEAD' else set()
    for path in changed_files:
        candidate = Path(path)
        if head == 'HEAD':
            if not candidate.is_file() or candidate.is_symlink():
                continue
            if path in untracked:
                result = subprocess.run(['git', 'diff', '--no-index', '--check', '/dev/null', path], check=False)
                failed |= result.returncode != 0
            content = candidate.read_bytes()
        else:
            result = subprocess.run(['git', 'show', f'{head}:{path}'], capture_output=True, check=False)
            if result.returncode:
                # Deleted files have no final content to scan.
                continue
            content = result.stdout
        try:
            lines = content.decode('utf-8').splitlines()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(lines, 1):
            if line.startswith(('<<<<<<<', '>>>>>>>')):
                print(f'{path}:{lineno}: unresolved merge conflict marker', file=sys.stderr)
                failed = True
    if failed:
        return 1
    print(
        'Fork diff hygiene passed; incorporated upstream whitespace preserved, all changed text scanned for conflicts.'
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--changed-files', required=True, type=Path)
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', default='HEAD')
    parser.add_argument('--upstream-ref', default='upstream/main')
    args = parser.parse_args()
    try:
        paths = args.changed_files.read_text(encoding='utf-8').splitlines()
        return check(paths, args.base, args.head, args.upstream_ref)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f'FAIL: cannot determine fork diff provenance: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
