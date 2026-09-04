"""Explicit PostgreSQL schema owner: python -m fork.migrate migrate|check."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('migrate', 'check'))
    args = parser.parse_args(argv)
    if not os.environ.get('FIRESTORE_PG_DSN', '').strip():
        parser.error('FIRESTORE_PG_DSN is required; migration never selects a default database')

    from firestore_pg.migrations import check_schema, migrate

    status = migrate() if args.command == 'migrate' else check_schema()
    print(json.dumps({'status': 'current', **asdict(status)}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
