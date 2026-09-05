#!/usr/bin/env python3
"""Measure whether a fork branch can merge the current upstream without mutation.

The scheduled sync job and its regression test both use this small wrapper
around ``git merge-tree``.  It does not create a branch, alter the index, or
reuse ``rerere``.  A caller can therefore report the exact conflict paths
before deciding whether a normal merge PR is safe to create.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SyncPlan:
    base: str
    upstream: str
    base_commit: str
    upstream_commit: str
    conflicts: list[str]


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def commit(ref: str) -> str:
    proc = run_git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    if proc.returncode:
        raise RuntimeError(f"cannot resolve {ref}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def conflict_paths(merge_tree_output: str) -> list[str]:
    paths = re.findall(r"^CONFLICT \([^)]*\): Merge conflict in (.+)$", merge_tree_output, flags=re.MULTILINE)
    return sorted(set(paths))


def make_plan(base: str, upstream: str) -> SyncPlan:
    base_commit = commit(base)
    upstream_commit = commit(upstream)
    proc = run_git(
        "-c", "rerere.enabled=false", "merge-tree", "--write-tree", "--messages", base_commit, upstream_commit
    )
    conflicts = conflict_paths(proc.stdout)
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"git merge-tree failed: {proc.stderr.strip() or proc.stdout.strip()}")
    if proc.returncode == 1 and not conflicts:
        raise RuntimeError(f"git merge-tree reported an unparseable conflict: {proc.stdout.strip()}")
    return SyncPlan(
        base=base,
        upstream=upstream,
        base_commit=base_commit,
        upstream_commit=upstream_commit,
        conflicts=conflicts,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--upstream", default="upstream/main")
    args = parser.parse_args()
    try:
        print(json.dumps(asdict(make_plan(args.base, args.upstream)), sort_keys=True))
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
