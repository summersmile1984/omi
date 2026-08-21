#!/usr/bin/env python3
"""Explicit schema and Firestore import owner for firestore_pg."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from google.api_core.client_options import ClientOptions

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Bind the real source SDK before importing firestore_pg migrations. Importing
# the database package can install the PostgreSQL facade when FIRESTORE_PG_DSN
# is set, but this module variable must continue to address the source.
from google.cloud import firestore as cloud_firestore

from firestore_pg.importer import run_import
from firestore_pg.migrations import check_schema, migrate, provision_collections
from scripts.source_write_freeze import SourceWriteFreezeError, canonical_endpoint, verify_lease


def _schema_payload(status: Any) -> dict[str, Any]:
    return {
        'status': 'passed',
        'current_version': status.current_version,
        'latest_version': status.latest_version,
        'registered_collection_count': len(status.collections),
    }


def _require_private_credentials(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Firestore source credentials are missing or are not a regular file: {path}')
    if path.stat().st_mode & 0o077:
        raise ValueError(f'Firestore source credentials must be mode 0600 or stricter: {path}')
    return path


def _source_client(args: argparse.Namespace) -> Any:
    if args.source_credentials:
        credentials = _require_private_credentials(Path(args.source_credentials))
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = str(credentials.resolve())
    kwargs: dict[str, Any] = {'project': args.source_project}
    endpoint = canonical_endpoint(args.source_endpoint)
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
    importer.add_argument(
        '--source-endpoint',
        required=True,
        help='credential-free Firestore API authority used by the freeze lease (for example https://firestore.googleapis.com)',
    )
    importer.add_argument('--source-credentials')
    importer.add_argument('--checkpoint', type=Path, required=True)
    importer.add_argument(
        '--freeze-lease',
        type=Path,
        required=True,
        help=f'mode-0600 HMAC lease proving Firestore source writes are paused ({"OMI_SOURCE_WRITE_FREEZE_SECRET"})',
    )
    importer.add_argument('--checkpoint-interval', type=int, default=100)
    args = parser.parse_args()

    if args.command == 'import':
        source = _source_client(args)
        source_endpoint = canonical_endpoint(str(getattr(source, '_target', '') or ''))
        requested_endpoint = canonical_endpoint(args.source_endpoint)
        if source_endpoint != requested_endpoint:
            print('ERROR: Firestore client endpoint does not match --source-endpoint', file=sys.stderr)
            return 1

        def verify_source_write_freeze() -> None:
            verify_lease(
                args.freeze_lease,
                source_project=args.source_project,
                source_database=args.source_database or '(default)',
                source_endpoint=source_endpoint,
                required_scopes={'firestore'},
            )

        try:
            verify_source_write_freeze()
        except SourceWriteFreezeError as error:
            print(f'ERROR: {error}', file=sys.stderr)
            return 1
        migrate()
        try:
            result = run_import(
                source,
                args.checkpoint,
                checkpoint_interval=args.checkpoint_interval,
                freeze_guard=verify_source_write_freeze,
            )
        except SourceWriteFreezeError as error:
            print(f'ERROR: {error}', file=sys.stderr)
            return 1
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
