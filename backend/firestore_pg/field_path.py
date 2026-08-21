"""Strict Firestore simple-field-path parsing for SQL-backed queries."""

from __future__ import annotations

import re
from typing import Any

_SIMPLE_SEGMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_RESERVED_SEGMENT = re.compile(r'^__.*__$')


class UnsupportedFirestoreQuery(RuntimeError):
    """The query uses a Firestore surface the PostgreSQL shim cannot preserve."""


def parse_field_path(field_path: Any, *, allow_document_name: bool = True) -> tuple[str, ...]:
    """Parse the unquoted Firestore field-path subset used by this backend.

    Firestore also supports backtick-quoted field names with escapes. The shim
    intentionally fails those closed until it has a fully equivalent parser;
    only dot-separated simple identifiers and the complete ``__name__``
    sentinel are admitted to SQL generation.
    """
    if not isinstance(field_path, str) or not field_path:
        raise UnsupportedFirestoreQuery('firestore_pg field paths must be non-empty strings')
    if field_path == '__name__':
        if allow_document_name:
            return ('__name__',)
        raise UnsupportedFirestoreQuery('__name__ is only supported as the complete document-name field path')
    segments = tuple(field_path.split('.'))
    if not segments or any(
        not _SIMPLE_SEGMENT.fullmatch(segment) or _RESERVED_SEGMENT.fullmatch(segment) for segment in segments
    ):
        raise UnsupportedFirestoreQuery(
            f'firestore_pg does not support quoted, escaped, reserved, or non-simple field path {field_path!r}'
        )
    return segments
