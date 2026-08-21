#!/usr/bin/env python3
"""Explicit schema and Firestore import owner for firestore_pg."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from google.api_core.client_options import ClientOptions

# Bind the real source SDK before importing firestore_pg migrations. Importing
# the database package can install the PostgreSQL facade when FIRESTORE_PG_DSN
# is set, but this module variable must continue to address the source.
from google.cloud import firestore as cloud_firestore

from firestore_pg.importer import run_import
from firestore_pg.migrations import check_schema, migrate, provision_collections


def canonical_endpoint(raw: str | None) -> str | None:
    value = (raw or '').strip().rstrip('/')
    if not value:
        return None
    if '://' in value:
        value = value.split('://', 1)[1]
    return value.lower()


def _schema_payload(status: Any) -> dict[str, Any]:
    return {
        'status': 'passed',
        'current_version': status.current_version,
        'latest_version': status.latest_version,
        'registered_collection_count': len(status.collections),
    }


def _source_client(args: argparse.Namespace) -> Any:
    if args.source_credentials:
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = str(Path(args.source_credentials).resolve())
    kwargs: dict[str, Any] = {'project': args.source_project}
    endpoint = canonical_endpoint(getattr(args, 'source_endpoint', None))
    if endpoint:
        kwargs['client_options'] = ClientOptions(api_endpoint=endpoint)
    if args.source_database:
        kwargs['database'] = args.source_database
    return cloud_firestore.Client(**kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('migrate', help='apply all forward migrations, then check')
    subparsers.add_parser('check', help='read-only current-schema check')
    provision = subparsers.add_parser('provision', help='explicitly provision dynamic collection IDs')
    provision.add_argument('collection_ids', nargs='+')
    importer = subparsers.add_parser('import', help='capture/resume Firestore and reconcile PostgreSQL')
    importer.add_argument('--source-project', required=True)
    importer.add_argument('--source-database')
    importer.add_argument('--source-credentials')
    importer.add_argument('--source-endpoint')
    importer.add_argument('--checkpoint', type=Path, required=True)
    importer.add_argument('--checkpoint-interval', type=int, default=100)
    args = parser.parse_args()

    if args.command == 'import':
        source = _source_client(args)
        migrate()
        result = run_import(source, args.checkpoint, checkpoint_interval=args.checkpoint_interval)
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.command == 'migrate':
        status = migrate()
    elif args.command == 'check':
        status = check_schema()
    else:
        provision_collections(args.collection_ids)
        status = check_schema()
    print(json.dumps(_schema_payload(status), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
