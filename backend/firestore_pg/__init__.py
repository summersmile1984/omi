"""firestore_pg: drop-in Google Cloud Firestore SDK replacement backed by PostgreSQL.

This package exposes the Firestore API surface used by the Omi backend
(collection/document/query/transaction/field ops) and translates it to
PostgreSQL (JSONB documents + promoted query columns).

Module registration: importing ``firestore_pg.compat`` installs aliases so
that ``from google.cloud import firestore`` and
``from google.cloud.firestore_v1 import FieldFilter`` resolve to this package
without touching the 88 business modules in ``database/``.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

from .codec import decode_value
from .field_path import parse_field_path

# ---------------------------------------------------------------------------
# Field transforms (Firestore API-compatible sentinels)
# ---------------------------------------------------------------------------


class _FieldTransform:
    """Base for Firestore sentinel values (Increment/ArrayUnion/...)."""

    def __init__(self, value: Any) -> None:
        self.value = value


class Increment(_FieldTransform):
    """Atomic counter increment: translate to ``data->key + value``."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Increment({self.value!r})"


class ArrayUnion(_FieldTransform):
    """Append unique elements to a JSONB array."""

    def __repr__(self) -> str:  # pragma: no cover
        return f"ArrayUnion({self.value!r})"


class ArrayRemove(_FieldTransform):
    """Remove elements from a JSONB array."""

    def __repr__(self) -> str:  # pragma: no cover
        return f"ArrayRemove({self.value!r})"


class _DeleteSentinel:
    def __repr__(self) -> str:  # pragma: no cover
        return "DELETE_FIELD"


DELETE_FIELD = _DeleteSentinel()


class _ServerTimestampSentinel:
    def __repr__(self) -> str:  # pragma: no cover
        return "SERVER_TIMESTAMP"


SERVER_TIMESTAMP = _ServerTimestampSentinel()


# ---------------------------------------------------------------------------
# Query filter
# ---------------------------------------------------------------------------

_OPERATORS = {
    "==": "=",
    "!=": "<>",
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
    "in": "IN",
    "not-in": "NOT IN",
    "array-contains": "@>",  # JSONB containment: data->key @> '[value]'
    # underscore variant used by database/apps.py and friends
    "array_contains": "@>",
    "array-contains-any": "?|",  # data->key ?| ARRAY[...]
    "array_contains_any": "?|",
}


class FieldFilter:
    """Firestore-compatible field filter; translated to a SQL WHERE clause."""

    def __init__(self, field_path: str, op_string: str, value: Any) -> None:
        if op_string not in _OPERATORS:
            raise ValueError(f"Unsupported filter operator: {op_string!r}")
        parse_field_path(field_path)
        self.field_path = field_path
        self.op_string = op_string
        self.value = value

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, FieldFilter)
            and self.field_path == other.field_path
            and self.op_string == other.op_string
            and self.value == other.value
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"FieldFilter({self.field_path!r}, {self.op_string!r}, {self.value!r})"


class BaseCompositeFilter:
    """Firestore-compatible AND/OR composite filter.

    Mirrors ``google.cloud.firestore_v1.base_query.BaseCompositeFilter``
    (``.filters`` / ``.operator``) so business code that builds
    ``BaseCompositeFilter('AND', [FieldFilter(...), ...])`` works unchanged.
    Only ``AND`` is supported by the shim's SQL translation; ``OR`` raises.
    """

    AND = "AND"
    OR = "OR"

    def __init__(self, operator: str, filters: Sequence[FieldFilter]) -> None:
        self.operator = operator
        self.filters = list(filters)


def _ensure_filter(filter_like: Any) -> FieldFilter:
    if isinstance(filter_like, FieldFilter):
        return filter_like
    # Real google-cloud-firestore FieldFilter exposes the same surface
    # (field_path / op_string / value) — accept it duck-typed so modules that
    # bind the real SDK (imported before compat.install) still work.
    if hasattr(filter_like, "op_string") and hasattr(filter_like, "field_path"):
        return FieldFilter(filter_like.field_path, filter_like.op_string, filter_like.value)
    if isinstance(filter_like, (list, tuple)) and len(filter_like) == 3:
        return FieldFilter(filter_like[0], filter_like[1], filter_like[2])
    raise TypeError(f"Cannot interpret {filter_like!r} as a FieldFilter")


# ---------------------------------------------------------------------------
# Timestamp coercion (Firestore SDK returns datetimes for Timestamp fields)
# ---------------------------------------------------------------------------


def _coerce_value_for_read(value: Any) -> Any:
    """Normalize a value read from JSONB into the shapes business code expects.

    Special Firestore values use explicit tagged JSON. Plain strings, including
    ISO-8601-looking user text, are never inferred or changed.
    """
    return decode_value(value)


# ---------------------------------------------------------------------------
# Real-SDK transform normalization
# ---------------------------------------------------------------------------
#
# Business modules bind ``firestore.ArrayUnion`` / ``firestore.DELETE_FIELD``
# / ``firestore.SERVER_TIMESTAMP`` at import time. When they were imported
# before ``compat.install()`` ran (e.g. via ``from google.cloud import
# firestore`` at module top), those names are the REAL google-cloud-firestore
# objects. Normalize them to our sentinels at the write boundary so isinstance
# checks in client.py keep working.

try:  # pragma: no cover - environment-dependent
    from google.cloud.firestore_v1 import transforms as _real_transforms  # type: ignore

    _REAL_ARRAY_UNION = _real_transforms.ArrayUnion
    _REAL_ARRAY_REMOVE = _real_transforms.ArrayRemove
    _REAL_INCREMENT = _real_transforms.Increment
    _REAL_SERVER_TIMESTAMP = _real_transforms.SERVER_TIMESTAMP
    _REAL_DELETE_FIELD = _real_transforms.DELETE_FIELD
except Exception:  # pragma: no cover - real SDK absent (fresh installs)
    _REAL_ARRAY_UNION = None
    _REAL_ARRAY_REMOVE = None
    _REAL_INCREMENT = None
    _REAL_SERVER_TIMESTAMP = None
    _REAL_DELETE_FIELD = None


def _normalize_transform(value: Any) -> Any:
    """Map a real-SDK transform object to our sentinel (identity if already ours)."""
    if _REAL_ARRAY_UNION is not None and isinstance(value, _REAL_ARRAY_UNION):
        return ArrayUnion(value._values)
    if _REAL_ARRAY_REMOVE is not None and isinstance(value, _REAL_ARRAY_REMOVE):
        return ArrayRemove(value._values)
    if _REAL_INCREMENT is not None and isinstance(value, _REAL_INCREMENT):
        return Increment(value._value)
    if _REAL_SERVER_TIMESTAMP is not None and value is _REAL_SERVER_TIMESTAMP:
        return SERVER_TIMESTAMP
    if _REAL_DELETE_FIELD is not None and value is _REAL_DELETE_FIELD:
        return DELETE_FIELD
    return value


def _is_real_delete_field(value: Any) -> bool:
    return _REAL_DELETE_FIELD is not None and value is _REAL_DELETE_FIELD
