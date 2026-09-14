#!/usr/bin/env python3
"""Render public assistant identity from the validated selected brand manifest."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts/profiles'), str(ROOT / 'backend')]
from render import load_manifest  # noqa: E402
from fork.brand import from_manifest  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = load_manifest(None, ROOT, args.manifest)
    identity = from_manifest(manifest, json.loads(args.profile.read_text()))
    args.output.write_text(json.dumps(identity.as_dict(), ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
