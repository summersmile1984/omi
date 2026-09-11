#!/usr/bin/env python3
"""Publish the retained release journal for a failed CD run.

A failed release used to leave one line -- "release did not pass readiness;
inspect recovery plan" -- and a host path. The journal held the evidence, but
only on the machine that ran the deployment, so diagnosing a failure meant
logging in and reconstructing which contract broke.

This runs as a `if: failure()` step in both CD workflows. It finds the journals
that belong to the exact admitted source, copies them into the run's artifact
directory, and writes a short markdown block into the job summary. The journal's
`failure.reason` is written by the transaction owner already redacted, so
nothing here has to know which values are secret.

The step must never mask the failure it is reporting, so the command always
exits 0: a missing or unreadable journal is reported in the summary instead of
replacing the real error with a second one.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Journal layouts differ per target. Both are one directory deep, and the search
# is enumerated rather than recursive on purpose: the Server stage root also
# holds retained Git checkouts, and walking them would turn a failure report into
# a multi-second scan.
#   cloudflare   <stage>-<sha>-<transaction>/journal.json
#   self_hosted  releases/<sha>/journal.json
#                qualifications/<sha>-<uuid>/journal.json
JOURNAL_PATTERNS = {
    'cloudflare': ('*/journal.json',),
    'self_hosted': ('releases/*/journal.json', 'qualifications/*/journal.json'),
}


def find_journals(root: Path, sha: str, target: str) -> list[Path]:
    if not root.is_dir():
        return []
    found = set()
    for pattern in JOURNAL_PATTERNS[target]:
        for path in root.glob(pattern):
            if path.is_file() and sha in str(path.relative_to(root)):
                found.add(path)
    return sorted(found)


def describe(journal: Path) -> list[str]:
    try:
        data = json.loads(journal.read_text())
    except (OSError, ValueError) as error:
        return [f"- `{journal.parent.name}`: could not be read ({error})"]
    if not isinstance(data, dict):
        return [f"- `{journal.parent.name}`: not a journal object"]
    failure = data.get('failure')
    lines = [
        f"- `{journal.parent.name}` state=`{data.get('state')}` "
        f"release_ready={data.get('release_ready')}"
    ]
    if isinstance(failure, dict):
        lines.append(
            f"  - target=`{failure.get('target')}` stage=`{failure.get('stage')}` "
            f"error=`{failure.get('error_name')}`"
        )
        lines.append(f"  - reason: {failure.get('reason')}")
    else:
        lines.append(
            "  - no `failure` block: the journal predates failure recording, or "
            "the run failed before the release transaction started"
        )
    return lines


def render(target: str, stage: str, sha: str, journals: list[Path], root: Path) -> str:
    lines = [
        f"### {target} release failure evidence",
        "",
        f"- stage: `{stage}`",
        f"- source: `{sha}`",
        f"- journal root: `{root}`",
        "",
    ]
    if not journals:
        lines.append(
            "No retained journal matched this source. The failing step log above "
            "is the only evidence, and it means the failure happened before the "
            "release transaction wrote a journal."
        )
        return '\n'.join(lines) + '\n'
    for journal in journals:
        lines.extend(describe(journal))
    return '\n'.join(lines) + '\n'


def collect(journals: list[Path], evidence_dir: Path) -> list[Path]:
    if not journals:
        return []
    evidence_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for journal in journals:
        destination = evidence_dir / f'{journal.parent.name}.json'
        try:
            shutil.copyfile(journal, destination)
        except OSError as error:
            print(f'NOTE: could not copy {journal}: {error}', file=sys.stderr)
            continue
        copied.append(destination)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', required=True, choices=['cloudflare', 'self_hosted'])
    parser.add_argument('--stage', required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--journal-root', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--summary', type=Path, help='append the markdown report here (default: stdout)')
    args = parser.parse_args()

    journals = find_journals(args.journal_root, args.sha, args.target)
    report = render(args.target, args.stage, args.sha, journals, args.journal_root)
    copied = collect(journals, args.evidence_dir)

    if args.summary:
        with args.summary.open('a', encoding='utf-8') as output:
            output.write(report)
    else:
        print(report, end='')
    print(
        f'release failure evidence: {len(journals)} journal(s) matched, {len(copied)} copied to {args.evidence_dir}',
        file=sys.stderr,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
