#!/usr/bin/env python3
"""Enforce the fork's zero-upstream-touch policy.

Every upstream file this fork modifies is a future merge conflict; every new
file, alias or import-time patch is not. So the default is zero: a change to a
file that exists in the upstream tree fails unless the file is listed in
`dev/unified-main/upstream-touch-allowlist.yaml` and stays within its line
budget. Some paths can never be allowlisted at all (upstream tests, lockfiles,
generated output, bot-written files, CI workflows) because a fork edit there is
guaranteed to be re-conflicted or overwritten.

Rationale and the per-platform techniques that replace an upstream edit:
`dev/unified-main/00-upstream-touch-policy.md`.

Usage:
    scripts/fork/check-upstream-touch.py [--base REF] [--head REF]
                                         [--upstream-ref REF] [--allowlist PATH]
                                         [--json]

Exit codes: 0 clean, 1 violations found, 2 could not evaluate.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ALLOWLIST = Path("dev/unified-main/upstream-touch-allowlist.yaml")
DEFAULT_UPSTREAM_REF = "upstream/main"
POLICY_DOC = "dev/unified-main/00-upstream-touch-policy.md"

# How to avoid touching an upstream file, per area. Printed with each violation
# so the failure tells the author what to do instead of only what went wrong.
REMEDIES: list[tuple[str, str]] = [
    ("backend/", "put the code in backend/fork/ and attach it with an import-time patch "
                 "(backend/fork/patches/); upstream modules stay byte-identical"),
    ("app/lib/", "use app/pubspec_overrides.yaml to point a package at a fork shim, or a "
                 "generated table under app/lib/env/fork/; do not edit call sites"),
    ("desktop/windows/", "extend the upstream config from desktop/windows/vite.fork.config.ts "
                         "with resolve.alias; do not edit call sites"),
    ("desktop/macos/", "emit desktop/macos/Desktop/Sources/Generated/*.swift and write Info.plist "
                       "keys at build time; only the allowlisted seams may change"),
    ("web/", "add a fork module under src/lib/fork/ and read it from an allowlisted seam"),
    ("omi/firmware/", "layer a Zephyr EXTRA_CONF_FILE/overlay instead of editing sources"),
    (".github/", "fork checks go in .github/checks-manifest.fork.yaml and .github/workflows/fork-*.yml"),
]

# Paths a fork must never modify, allowlist or not.
FORBIDDEN: list[tuple[str, str]] = [
    ("**/tests/**", "upstream tests run unmodified in upstream mode; assert fork behavior in fork test dirs"),
    ("**/test/**", "upstream tests run unmodified in upstream mode; assert fork behavior in fork test dirs"),
    ("**/Tests/**", "upstream tests run unmodified in upstream mode; assert fork behavior in fork test dirs"),
    ("**/*_test.py", "upstream tests run unmodified in upstream mode"),
    ("**/*.test.ts", "upstream tests run unmodified in upstream mode"),
    ("**/*.test.tsx", "upstream tests run unmodified in upstream mode"),
    ("**/*.test.mjs", "upstream tests run unmodified in upstream mode"),
    ("backend/pylock*.toml", "fork dependencies go in backend/requirements-fork.txt, installed by the fork image layer"),
    ("backend/requirements.txt", "fork dependencies go in backend/requirements-fork.txt"),
    ("backend/*/requirements.txt", "fork dependencies go in backend/requirements-fork.txt"),
    ("app/pubspec.lock", "use app/pubspec_overrides.yaml instead"),
    ("web/*/package-lock.json", "use a fork-owned package.json under deploy/"),
    ("**/*.g.dart", "generated output is regenerated, never hand-edited or committed as a fork diff"),
    ("**/*.gen.dart", "generated output is regenerated, never hand-edited"),
    ("app/lib/l10n/app_localizations*.dart", "generated output; change the ARB or use a runtime delegate"),
    (".github/guardrail-pulse-history.jsonl", "bot-written; the fork's pulse workflow is disabled and this file takes upstream"),
    ("desktop/macos/CHANGELOG.json", "bot-written by the release train; add a changelog fragment instead"),
    ("community-plugin-stats.json", "bot-written upstream snapshot"),
    (".github/workflows/*.yml", "fork workflows use new .github/workflows/fork-*.yml files; upstream ones are disabled in the fork, not edited"),
    (".github/checks-manifest.yaml", "fork checks go in .github/checks-manifest.fork.yaml"),
    ("config/deployment-setting-classification.json", "fork settings go in config/deployment-setting-classification.fork.json"),
]

# Fork-owned workflow files are exempt from the workflow rule above.
FORBIDDEN_EXEMPT: tuple[str, ...] = (
    ".github/workflows/fork-",
)


@dataclass
class AllowEntry:
    path: str
    max_added_lines: int
    reason: str = ""
    upstream_pr: str | None = None


@dataclass
class Violation:
    path: str
    kind: str
    detail: str
    remedy: str = ""


@dataclass
class Result:
    violations: list[Violation] = field(default_factory=list)
    checked: int = 0
    allowed: list[str] = field(default_factory=list)


def run_git(args: list[str]) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def parse_allowlist(path: Path) -> tuple[list[AllowEntry], list[str]]:
    """Parse the allowlist without a YAML dependency.

    The file is intentionally a flat, hand-written subset: a `forbidden_patterns`
    list of scalars and an `allow` list of `- path:` records. Keeping the parser
    here means the guard runs on a bare Python with no install step, the same
    property the upstream check runner relies on.
    """
    if not path.exists():
        raise RuntimeError(f"allowlist not found: {path}")
    entries: list[AllowEntry] = []
    extra_forbidden: list[str] = []
    section = None
    current: dict[str, str] = {}

    def flush() -> None:
        if current.get("path"):
            entries.append(
                AllowEntry(
                    path=current["path"],
                    max_added_lines=int(current.get("max_added_lines", "0") or 0),
                    reason=current.get("reason", ""),
                    upstream_pr=current.get("upstream_pr") or None,
                )
            )
        current.clear()

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        if line.startswith("forbidden_patterns:"):
            flush()
            section = "forbidden"
            continue
        if line.startswith("allow:"):
            flush()
            section = "allow"
            continue
        if not line.startswith(" "):
            flush()
            section = None
            continue
        stripped = line.strip()
        if section == "forbidden" and stripped.startswith("- "):
            extra_forbidden.append(stripped[2:].strip().strip('"').strip("'"))
        elif section == "allow":
            if stripped.startswith("- path:"):
                flush()
                current["path"] = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            elif ":" in stripped:
                key, value = stripped.split(":", 1)
                current[key.strip()] = value.strip().strip('"').strip("'")
    flush()
    return entries, extra_forbidden


def matches(path: str, pattern: str) -> bool:
    # "dir/**" is a literal prefix only when the prefix itself has no wildcard;
    # "**/tests/**" must fall through to fnmatch, whose "*" spans "/".
    prefix = pattern[:-2]
    if pattern.endswith("/**") and "*" not in prefix:
        return path.startswith(prefix)
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, f"*/{pattern}")


def remedy_for(path: str) -> str:
    for prefix, text in REMEDIES:
        if path.startswith(prefix):
            return text
    return "keep the change in a fork-owned path; see " + POLICY_DOC


def added_lines(base: str, head: str, path: str) -> int:
    out = run_git(["diff", "--numstat", f"{base}...{head}", "--", path]).strip()
    if not out:
        return 0
    total = 0
    for line in out.splitlines():
        added = line.split("\t", 1)[0]
        if added.isdigit():
            total += int(added)
    return total


def evaluate(base: str, head: str, upstream_ref: str, allowlist_path: Path) -> Result:
    entries, extra_forbidden = parse_allowlist(allowlist_path)
    allowed_by_path = {e.path: e for e in entries}

    changed = [p for p in run_git(["diff", "--name-only", f"{base}...{head}"]).splitlines() if p]
    upstream_files = set(run_git(["ls-tree", "-r", "--name-only", upstream_ref]).splitlines())

    result = Result()
    forbidden_rules = list(FORBIDDEN) + [(p, "listed under forbidden_patterns in the allowlist") for p in extra_forbidden]

    for path in changed:
        if path not in upstream_files:
            continue  # fork-owned path: always fine
        result.checked += 1

        exempt = any(path.startswith(prefix) for prefix in FORBIDDEN_EXEMPT)
        hit = None if exempt else next(((pat, why) for pat, why in forbidden_rules if matches(path, pat)), None)
        if hit is not None:
            result.violations.append(
                Violation(path, "forbidden", f"matches never-modify rule '{hit[0]}': {hit[1]}", remedy_for(path))
            )
            continue

        entry = allowed_by_path.get(path)
        if entry is None:
            result.violations.append(
                Violation(path, "not-allowlisted", "upstream file modified with no allowlist entry", remedy_for(path))
            )
            continue

        n = added_lines(base, head, path)
        if n > entry.max_added_lines:
            result.violations.append(
                Violation(
                    path,
                    "over-budget",
                    f"added {n} lines, allowlist budget is {entry.max_added_lines}",
                    "shrink the hook to a single call that reads config, or move the logic to a fork path",
                )
            )
            continue
        result.allowed.append(f"{path} (+{n}/{entry.max_added_lines})")

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main", help="merge base side of the diff (default: origin/main)")
    parser.add_argument("--head", default="HEAD", help="head side of the diff (default: HEAD)")
    parser.add_argument("--upstream-ref", default=DEFAULT_UPSTREAM_REF,
                        help=f"ref whose tree defines 'upstream file' (default: {DEFAULT_UPSTREAM_REF})")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args()

    try:
        run_git(["rev-parse", "--verify", args.upstream_ref])
    except RuntimeError:
        # Without the upstream ref there is no definition of "upstream file";
        # skip rather than pass silently, so a missing remote cannot look clean.
        message = (
            f"SKIP: {args.upstream_ref} is not available; cannot classify upstream files.\n"
            "      Run: git remote add upstream https://github.com/BasedHardware/omi.git && git fetch upstream main"
        )
        print(json.dumps({"ok": None, "skipped": message}) if args.json else message)
        return 0

    try:
        result = evaluate(args.base, args.head, args.upstream_ref, Path(args.allowlist))
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "ok": not result.violations,
            "upstream_files_changed": result.checked,
            "allowed": result.allowed,
            "violations": [v.__dict__ for v in result.violations],
        }, indent=2))
        return 1 if result.violations else 0

    if not result.violations:
        print(f"OK: {result.checked} upstream file(s) changed, all within the allowlist.")
        for line in result.allowed:
            print(f"  allowed  {line}")
        return 0

    print("FAIL: the fork must not modify upstream files outside the allowlist.\n")
    for v in result.violations:
        print(f"  {v.path}")
        print(f"    {v.kind}: {v.detail}")
        if v.remedy:
            print(f"    instead: {v.remedy}")
        print()
    print(f"Policy and per-platform techniques: {POLICY_DOC}")
    print(f"Allowlist (only-shrinks): {args.allowlist}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
