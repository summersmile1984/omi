#!/usr/bin/env python3
"""Rebuild or reconcile the self-hosted conversation Typesense projection."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.conversations.typesense_index import rebuild_conversation_index, reconcile_conversation_index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    rebuild = subparsers.add_parser('rebuild', help='replace the complete Typesense projection from Firestore')
    rebuild.add_argument('--batch-size', type=int, default=200)
    subparsers.add_parser('reconcile', help='compare Firestore and Typesense count/content hashes')
    args = parser.parse_args()

    if args.command == 'rebuild':
        indexed_count = rebuild_conversation_index(batch_size=args.batch_size)
        report = reconcile_conversation_index()
        print(json.dumps({'indexed_count': indexed_count, 'reconciliation': report.to_dict()}, sort_keys=True))
        return 0 if report.matches else 2

    report = reconcile_conversation_index()
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.matches else 2


if __name__ == '__main__':
    raise SystemExit(main())
