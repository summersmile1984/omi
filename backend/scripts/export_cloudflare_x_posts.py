"""Export explicitly selected Firestore X posts for the reviewed D1 backfill.

The export contains raw user-authored post text. It is therefore written only
to a new mode-0600 file, never stdout, and never logs identities or content.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Protocol, TypedDict

MAX_EXPORT_ROWS = 5_000
MAX_TEXT_BYTES = 100_000
VALID_KINDS = frozenset({'tweet', 'bookmark', 'like'})
VALID_EXTRACTION_STATES = frozenset({'pending', 'completed'})


class DocumentSnapshot(Protocol):
    id: str

    def to_dict(self) -> Mapping[str, Any] | None: ...


class FirestoreClient(Protocol):
    def collection(self, name: str) -> Any: ...


class ExportRecord(TypedDict):
    table: str
    row: dict[str, object]


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or '/' in value or '\x00' in value:
        raise ValueError(f'{field} is invalid')
    return value


def _timestamp(value: object, field: str, *, required: bool = False) -> object | None:
    if value is None:
        if required:
            raise ValueError(f'{field} is required')
        return None
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or value < 0 or not float(value).is_integer():
            raise ValueError(f'{field} is invalid')
        return int(value)
    if isinstance(value, str) and value.strip():
        normalized = value.strip()
        try:
            datetime.fromisoformat(normalized.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError(f'{field} is invalid') from None
        return normalized
    raise ValueError(f'{field} is invalid')


def normalize_snapshot(uid: str, snapshot: DocumentSnapshot) -> ExportRecord:
    normalized_uid = _identifier(uid, 'uid')
    post_id = _identifier(snapshot.id, 'post id')
    raw = snapshot.to_dict()
    if not isinstance(raw, Mapping):
        raise ValueError('X post document is not an object')
    raw_id = raw.get('id')
    if raw_id is not None and str(raw_id) != post_id:
        raise ValueError('X post field id does not match its document id')

    text = raw.get('text')
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > MAX_TEXT_BYTES:
        raise ValueError('X post text is invalid')
    kind = raw.get('kind', 'tweet')
    if kind not in VALID_KINDS:
        raise ValueError('X post kind is invalid')
    lang = raw.get('lang')
    if lang is not None and (not isinstance(lang, str) or len(lang) > 32):
        raise ValueError('X post language is invalid')
    metrics = raw.get('metrics')
    if metrics is None:
        metrics = raw.get('public_metrics')
    if metrics is None:
        metrics = {}
    if not isinstance(metrics, Mapping):
        raise ValueError('X post metrics are invalid')
    # JSON encoding here proves the payload has no Firestore-only values before
    # it reaches the generic D1 SQL generator.
    json.dumps(metrics, ensure_ascii=False, separators=(',', ':'), sort_keys=True)

    created_at = _timestamp(raw.get('created_at'), 'created_at', required=True)
    ingested_at = _timestamp(raw.get('ingested_at'), 'ingested_at')
    updated_at = _timestamp(raw.get('updated_at'), 'updated_at') or ingested_at or created_at
    extraction_state = raw.get('memory_extraction_status', 'pending')
    if extraction_state not in VALID_EXTRACTION_STATES:
        raise ValueError('X post memory extraction status is invalid')
    extracted_at = _timestamp(raw.get('memory_extracted_at'), 'memory_extracted_at')

    row: dict[str, object] = {
        'uid': normalized_uid,
        'id': post_id,
        'text': text,
        'kind': kind,
        'metrics': dict(metrics),
        'created_at': created_at,
        'updated_at': updated_at,
        'memory_extraction_status': extraction_state,
    }
    if lang is not None:
        row['lang'] = lang
    if ingested_at is not None:
        row['ingested_at'] = ingested_at
    if extracted_at is not None:
        row['memory_extracted_at'] = extracted_at
    return {'table': 'cf_x_posts', 'row': row}


def collect_records(client: FirestoreClient, uids: Iterable[str], *, max_rows: int) -> list[ExportRecord]:
    if not 1 <= max_rows <= MAX_EXPORT_ROWS:
        raise ValueError(f'max_rows must be between 1 and {MAX_EXPORT_ROWS}')
    selected_uids = sorted({_identifier(uid, 'uid') for uid in uids})
    if not selected_uids:
        raise ValueError('at least one uid is required')

    records: list[ExportRecord] = []
    users = client.collection('users')
    for uid in selected_uids:
        snapshots = users.document(uid).collection('x_posts').stream()
        for snapshot in snapshots:
            records.append(normalize_snapshot(uid, snapshot))
            if len(records) > max_rows:
                raise ValueError(f'export exceeds the {max_rows}-row limit')
    records.sort(key=lambda record: (str(record['row']['uid']), str(record['row']['id'])))
    return records


def render_jsonl(records: Iterable[Mapping[str, object]]) -> bytes:
    lines = [json.dumps(record, ensure_ascii=False, separators=(',', ':'), sort_keys=True) for record in records]
    return (('\n'.join(lines) + '\n') if lines else '').encode('utf-8')


def write_private_export(path: Path, payload: bytes) -> None:
    path = path.expanduser().resolve()
    if not path.parent.is_dir():
        raise ValueError('output directory does not exist')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        # Do not hide a partial sensitive export. Its mode remains 0600 and the
        # operator can remove the exact path after inspecting the failure.
        raise


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Export selected Firestore X posts for Cloudflare D1')
    parser.add_argument('--uid', action='append', required=True, help='exact user id; repeat for multiple users')
    parser.add_argument('--output', type=Path, required=True, help='new JSONL file; must not already exist')
    parser.add_argument('--max-rows', type=int, default=MAX_EXPORT_ROWS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv if argv is not None else sys.argv[1:]))
    if not 1 <= args.max_rows <= MAX_EXPORT_ROWS:
        raise SystemExit(f'--max-rows must be between 1 and {MAX_EXPORT_ROWS}')
    from database._client import get_firestore_client

    records = collect_records(get_firestore_client(), args.uid, max_rows=args.max_rows)
    payload = render_jsonl(records)
    write_private_export(args.output, payload)
    print(
        json.dumps(
            {
                'rows': len(records),
                'uids': len(set(args.uid)),
                'sha256': hashlib.sha256(payload).hexdigest(),
            },
            separators=(',', ':'),
            sort_keys=True,
        )
    )
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'X post export failed: {type(error).__name__}', file=sys.stderr)
        raise SystemExit(1) from None
