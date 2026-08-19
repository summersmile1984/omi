"""Shadow-diff runner: execute the same Firestore API scenario sequence against
the real SDK (Firestore emulator) and the firestore_pg shim (PostgreSQL), then
diff the normalized JSON results.

Usage:
  # real SDK against the emulator (no FIRESTORE_PG_DSN set)
  FIRESTORE_EMULATOR_HOST=localhost:8080 FIREBASE_PROJECT_ID=demo-omi-local \
    python shadow_diff.py --mode real --out /tmp/shadow-real.json

  # shim against PostgreSQL
  FIRESTORE_PG_DSN=postgresql+psycopg://omi:... python shadow_diff.py --mode shim \
    --out /tmp/shadow-shim.json

  # diff
  python shadow_diff.py --diff /tmp/shadow-real.json /tmp/shadow-shim.json

A successful diff exits 0 and prints a summary; mismatches print a unified diff
per scenario and exit 1.
"""

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from firestore_pg.tests.shadow_scenarios import SCENARIOS  # noqa: E402


def _run_scenarios(db: Any) -> dict:
    results = {}
    for name, scenario in SCENARIOS.items():
        try:
            results[name] = {"ok": True, "value": scenario(db)}
        except Exception as exc:  # noqa: BLE001 - report per-scenario failure
            results[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return results


def _make_client(mode: str):
    if mode == "real":
        from google.cloud import firestore

        return firestore.Client()
    if mode == "shim":
        from firestore_pg.compat import install

        install()
        from google.cloud import firestore

        return firestore.Client()
    raise ValueError(f"unknown mode {mode!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["real", "shim"])
    parser.add_argument("--out")
    parser.add_argument("--diff", nargs=2, metavar=("A", "B"))
    args = parser.parse_args()

    if args.diff:
        with open(args.diff[0]) as f:
            a = json.load(f)
        with open(args.diff[1]) as f:
            b = json.load(f)
        mismatches = []
        for name in a:
            if a.get(name) != b.get(name):
                mismatches.append(name)
        if not mismatches:
            print(f"SHADOW DIFF OK: {len(a)} scenarios matched")
            return 0
        print(f"SHADOW DIFF MISMATCH: {len(mismatches)}/{len(a)} scenarios differ")
        for name in mismatches:
            print(f"\n=== {name} ===")
            import difflib

            for line in difflib.unified_diff(
                json.dumps(a[name], indent=2, sort_keys=True).splitlines(),
                json.dumps(b[name], indent=2, sort_keys=True).splitlines(),
                fromfile=f"{args.diff[0]}:{name}",
                tofile=f"{args.diff[1]}:{name}",
            ):
                print(line)
        return 1

    if not args.mode or not args.out:
        parser.error("--mode and --out required (or --diff)")
    client = _make_client(args.mode)
    results = _run_scenarios(client)
    with open(args.out, "w") as f:
        json.dump(results, f, indent=2, sort_keys=True, default=str)
    ok = sum(1 for r in results.values() if r["ok"])
    print(f"{args.mode}: {ok}/{len(results)} scenarios ok -> {args.out}")
    for name, r in results.items():
        if not r["ok"]:
            print(f"  FAIL {name}: {r['error']}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
