#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Validate the operator egress-policy evidence used by external cutover.

The policy is deliberately a small JSON contract rather than a claimed
cryptographic signature.  The actual socket probes remain the behavioral
evidence; this validator prevents an arbitrary non-empty file from being
accepted as if it described the required deny boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
EXPECTED_ENFORCEMENT = "network_default_deny"
EXPECTED_WORKLOADS = ["auth-server", "backend", "queue-worker"]
EXPECTED_DENIED_TARGETS = [
    "1.1.1.1",
    "api.openai.com",
    "api.omi.me",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
]
EXPECTED_KEYS = {
    "schema_version",
    "enforcement",
    "workloads",
    "denied_targets",
    "source_git_commit",
    "source_git_tree",
    "runtime_config_sha256",
}


def validate_policy(payload: Any, *, expected_source: dict[str, str] | None = None) -> dict[str, Any]:
    """Return the normalized contract or raise ``ValueError``.

    Exact lists are intentional: the external gate probes the same workload
    and sentinel set, and silently accepting a partial policy would make the
    resulting production authorization claim ambiguous.
    """

    if not isinstance(payload, dict):
        raise ValueError("egress policy must be a JSON object")
    if set(payload) != EXPECTED_KEYS:
        raise ValueError(f"egress policy keys must be exactly {sorted(EXPECTED_KEYS)}")
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"egress policy schema_version must be {SCHEMA_VERSION}")
    if payload.get("enforcement") != EXPECTED_ENFORCEMENT:
        raise ValueError(f"egress policy enforcement must be {EXPECTED_ENFORCEMENT!r}")
    if payload.get("workloads") != EXPECTED_WORKLOADS:
        raise ValueError(f"egress policy workloads must be {EXPECTED_WORKLOADS}")
    if payload.get("denied_targets") != EXPECTED_DENIED_TARGETS:
        raise ValueError(f"egress policy denied_targets must be {EXPECTED_DENIED_TARGETS}")
    source = {
        "git_commit": payload.get("source_git_commit"),
        "git_tree": payload.get("source_git_tree"),
        "runtime_config_sha256": payload.get("runtime_config_sha256"),
    }
    if (
        not isinstance(source["git_commit"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", source["git_commit"])
        or not isinstance(source["git_tree"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", source["git_tree"])
        or not isinstance(source["runtime_config_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", source["runtime_config_sha256"])
    ):
        raise ValueError("egress policy source/config binding must contain full Git IDs and a SHA-256")
    if expected_source is not None and source != expected_source:
        raise ValueError("egress policy source/config binding does not match the tested runtime")
    return {
        "schema_version": SCHEMA_VERSION,
        "enforcement": EXPECTED_ENFORCEMENT,
        "workloads": EXPECTED_WORKLOADS,
        "denied_targets": EXPECTED_DENIED_TARGETS,
        "source_git_commit": source["git_commit"],
        "source_git_tree": source["git_tree"],
        "runtime_config_sha256": source["runtime_config_sha256"],
    }


def read_policy(path: Path, *, expected_source: dict[str, str] | None = None) -> tuple[dict[str, Any], str]:
    """Read a regular policy file and return normalized data plus SHA-256."""

    if not path.is_file() or path.is_symlink():
        raise ValueError("egress policy artifact must be a regular file")
    raw = path.read_bytes()
    if not raw:
        raise ValueError("egress policy artifact must not be empty")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("egress policy artifact must be valid UTF-8 JSON") from error
    return validate_policy(payload, expected_source=expected_source), hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--expected-git-commit", required=True)
    parser.add_argument("--expected-git-tree", required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        payload, digest = read_policy(
            args.path,
            expected_source={
                "git_commit": args.expected_git_commit,
                "git_tree": args.expected_git_tree,
                "runtime_config_sha256": args.expected_config_sha256,
            },
        )
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"policy": payload, "sha256": digest}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
