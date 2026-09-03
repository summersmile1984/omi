"""Unambiguous JSONB codec for Firestore values that JSON cannot represent."""

from __future__ import annotations

import base64
import math
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, cast

from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.cloud.firestore_v1 import GeoPoint

_TAG = '__firestore_pg_value__'
_VALUE = 'value'


class _StoredDatetimeWithNanoseconds(DatetimeWithNanoseconds):
    """Timestamp decoded for an internal read-modify-write cycle.

    Google API Core's ``timestamp_pb()`` moves negative fractional
    ``DatetimeWithNanoseconds`` values across the Unix epoch.  Values already
    stored in canonical Firestore form must keep their calendar instant when
    an unrelated field is updated, so this private subtype records that
    provenance without changing the public snapshot value type.
    """


@dataclass(frozen=True)
class FirestoreReferenceValue:
    """Readable, lossless document-reference value returned by the shim."""

    path: str

    @property
    def id(self) -> str:
        return self.path.rsplit('/', 1)[-1]


def _is_geo_point(value: Any) -> bool:
    return (
        type(value).__name__ == 'GeoPoint'
        and type(value).__module__.startswith('google.cloud.firestore')
        and hasattr(value, 'latitude')
        and hasattr(value, 'longitude')
    )


def _is_document_reference(value: Any) -> bool:
    return type(value).__name__ in {'DocumentReference', 'FirestoreReferenceValue'} and isinstance(
        getattr(value, 'path', None), str
    )


def encode_timestamp(value: datetime, *, preserve_calendar: bool = False) -> str:
    """Return Firestore timestamps as canonical UTC RFC3339 with 9 digits.

    Firestore persists timestamps at microsecond precision and truncates any
    smaller fraction. ``DatetimeWithNanoseconds`` must additionally follow the
    Google SDK's ``timestamp_pb()`` conversion: it uses toward-zero integral
    seconds for negative fractional epochs, unlike ordinary ``datetime``
    serialization. The canonical text is the comparison and persistence
    authority and renders nine fixed fractional digits so lexical and
    chronological order agree.
    """
    if (
        isinstance(value, DatetimeWithNanoseconds)
        and not isinstance(value, _StoredDatetimeWithNanoseconds)
        and not preserve_calendar
    ):
        wire = value.timestamp_pb()
        utc = datetime.fromtimestamp(wire.seconds, tz=timezone.utc).replace(microsecond=wire.nanos // 1_000)
    else:
        timestamp = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        utc = timestamp.astimezone(timezone.utc)
    nanosecond = utc.microsecond * 1_000
    return f'{utc:%Y-%m-%dT%H:%M:%S}.{nanosecond:09d}Z'


def unsupported_order_value_kind(value: Any) -> str | None:
    """Name values whose Firestore ordering is intentionally unsupported.

    Exact equality / membership remain lossless through the tagged codec.  We
    fail closed for range, ordering and cursors rather than let PostgreSQL's
    JSONB ordering silently differ from Firestore for these value families.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return 'bytes'
    if _is_geo_point(value):
        return 'GeoPoint'
    if _is_document_reference(value):
        return 'document reference'
    if isinstance(value, (list, tuple)):
        return 'array'
    if isinstance(value, Mapping):
        return 'map'
    if isinstance(value, float) and not math.isfinite(value):
        return 'non-finite float'
    if isinstance(value, str) and '\x00' in value:
        return 'string containing NUL'
    if isinstance(value, Mapping) and any(isinstance(key, str) and '\x00' in key for key in value):
        return 'map containing a NUL key'
    return None


def finite_double_numeric(value: float) -> str:
    """Return the exact base-10 value of one finite IEEE-754 double."""
    if not math.isfinite(value):
        raise ValueError('non-finite doubles have no PostgreSQL numeric representation')
    return str(Decimal.from_float(value))


def _encode_double(value: float) -> dict[str, Any]:
    if math.isnan(value):
        # Firestore normalizes every NaN and equality treats all NaNs alike.
        bits = '7ff8000000000000'
        numeric = 'nan'
    elif math.isinf(value):
        bits = struct.pack('>d', value).hex()
        numeric = 'infinity' if value > 0 else '-infinity'
    else:
        bits = struct.pack('>d', value).hex()
        numeric = finite_double_numeric(value)
    return {_TAG: 'double', _VALUE: {'bits': bits, 'numeric': numeric}}


def encode_value(value: Any, *, preserve_timestamp_calendar: bool = False) -> Any:
    """Encode one Firestore value into collision-free JSON-compatible data."""
    if isinstance(value, datetime):
        return {
            _TAG: 'timestamp',
            _VALUE: encode_timestamp(value, preserve_calendar=preserve_timestamp_calendar),
        }
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {_TAG: 'bytes', _VALUE: base64.b64encode(bytes(value)).decode('ascii')}
    if _is_geo_point(value):
        return {
            _TAG: 'geo_point',
            _VALUE: {
                'latitude': _encode_double(float(value.latitude)),
                'longitude': _encode_double(float(value.longitude)),
            },
        }
    if _is_document_reference(value):
        return {_TAG: 'document_reference', _VALUE: value.path}
    if isinstance(value, str):
        if '\x00' in value:
            return {_TAG: 'string_utf8', _VALUE: base64.b64encode(value.encode('utf-8')).decode('ascii')}
        return value
    if isinstance(value, Mapping):
        non_string_keys = [key for key in value if not isinstance(key, str)]
        if non_string_keys:
            raise TypeError('Firestore map keys must be strings')
        if any(key.startswith('__') and key.endswith('__') for key in value):
            raise TypeError('Firestore map field names matching __.*__ are reserved')
        if any('\x00' in key for key in value):
            entries = [
                [encode_value(key), encode_value(item, preserve_timestamp_calendar=preserve_timestamp_calendar)]
                for key, item in sorted(value.items(), key=lambda pair: pair[0].encode('utf-8'))
            ]
            return {_TAG: 'map_entries', _VALUE: entries}
        encoded = {
            key: encode_value(item, preserve_timestamp_calendar=preserve_timestamp_calendar)
            for key, item in value.items()
        }
        return encoded
    if isinstance(value, (list, tuple)):
        return [encode_value(item, preserve_timestamp_calendar=preserve_timestamp_calendar) for item in value]
    if isinstance(value, float):
        return _encode_double(value)
    if value is None or isinstance(value, (int, bool)):
        return value
    raise TypeError(f'unsupported Firestore value type: {type(value).__name__}')


def encode_document(document: Any, *, preserve_timestamp_calendar: bool = False) -> dict[str, Any]:
    """Encode document fields without treating the root as a map value.

    The root is structurally distinct from a nested map value. Encoding it
    through :func:`encode_value` would introduce an envelope and make SQL field
    paths point at the wrong level.
    """
    if not isinstance(document, Mapping):
        raise TypeError('Firestore document must be a mapping')
    raw_document = cast(Mapping[Any, Any], document)
    if any(not isinstance(key, str) for key in raw_document):
        raise TypeError('Firestore document field names must be strings')
    typed_document = cast(Mapping[str, Any], raw_document)
    if any(key.startswith('__') and key.endswith('__') for key in typed_document):
        raise TypeError('Firestore document field names matching __.*__ are reserved')
    if any('\x00' in key for key in typed_document):
        raise TypeError('Firestore document field names containing NUL are not supported by PostgreSQL JSONB')
    return {
        key: encode_value(value, preserve_timestamp_calendar=preserve_timestamp_calendar)
        for key, value in typed_document.items()
    }


def decode_value(
    value: Any,
    *,
    reference_factory: Callable[[str], Any] | None = None,
    _mark_stored_timestamps: bool = False,
) -> Any:
    """Decode JSONB data produced by :func:`encode_value`."""
    if isinstance(value, list):
        return [
            decode_value(
                item,
                reference_factory=reference_factory,
                _mark_stored_timestamps=_mark_stored_timestamps,
            )
            for item in value
        ]
    if not isinstance(value, Mapping):
        return value
    tag = value.get(_TAG)
    if tag == 'timestamp' and set(value) == {_TAG, _VALUE}:
        timestamp_type = _StoredDatetimeWithNanoseconds if _mark_stored_timestamps else DatetimeWithNanoseconds
        return timestamp_type.from_rfc3339(str(value[_VALUE]))
    if tag == 'bytes' and set(value) == {_TAG, _VALUE}:
        return base64.b64decode(str(value[_VALUE]), validate=True)
    if tag == 'string_utf8' and set(value) == {_TAG, _VALUE}:
        return base64.b64decode(str(value[_VALUE]), validate=True).decode('utf-8')
    if tag == 'geo_point' and set(value) == {_TAG, _VALUE}:
        point = value[_VALUE]
        if not isinstance(point, Mapping):
            raise ValueError('invalid stored Firestore GeoPoint')
        latitude = decode_value(point['latitude'])
        longitude = decode_value(point['longitude'])
        if not isinstance(latitude, float) or not isinstance(longitude, float):
            raise ValueError('invalid stored Firestore GeoPoint components')
        return GeoPoint(latitude, longitude)
    if tag == 'document_reference' and set(value) == {_TAG, _VALUE}:
        path = str(value[_VALUE])
        return reference_factory(path) if reference_factory is not None else FirestoreReferenceValue(path)
    if tag == 'nonfinite_float' and set(value) == {_TAG, _VALUE}:
        labels = {'nan': float('nan'), 'infinity': float('inf'), '-infinity': float('-inf')}
        label = str(value[_VALUE])
        if label not in labels:
            raise ValueError('invalid stored non-finite Firestore float')
        return labels[label]
    if tag == 'double' and set(value) == {_TAG, _VALUE}:
        encoded_double = value[_VALUE]
        if not isinstance(encoded_double, Mapping) or set(encoded_double) != {'bits', 'numeric'}:
            raise ValueError('invalid stored Firestore double')
        raw = bytes.fromhex(str(encoded_double['bits']))
        if len(raw) != 8:
            raise ValueError('invalid stored Firestore double bits')
        return struct.unpack('>d', raw)[0]
    if tag == 'map' and set(value) == {_TAG, _VALUE}:
        escaped = value[_VALUE]
        if not isinstance(escaped, Mapping):
            raise ValueError('invalid stored escaped Firestore map')
        return {
            str(key): decode_value(
                item,
                reference_factory=reference_factory,
                _mark_stored_timestamps=_mark_stored_timestamps,
            )
            for key, item in escaped.items()
        }
    if tag == 'map_entries' and set(value) == {_TAG, _VALUE}:
        entries = value[_VALUE]
        if not isinstance(entries, list):
            raise ValueError('invalid stored Firestore map entries')
        decoded: dict[str, Any] = {}
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError('invalid stored Firestore map entry')
            key = decode_value(
                entry[0],
                reference_factory=reference_factory,
                _mark_stored_timestamps=_mark_stored_timestamps,
            )
            if not isinstance(key, str) or key in decoded:
                raise ValueError('invalid or duplicate stored Firestore map key')
            decoded[key] = decode_value(
                entry[1],
                reference_factory=reference_factory,
                _mark_stored_timestamps=_mark_stored_timestamps,
            )
        return decoded
    return {
        str(key): decode_value(
            item,
            reference_factory=reference_factory,
            _mark_stored_timestamps=_mark_stored_timestamps,
        )
        for key, item in value.items()
    }


def decode_document(
    document: Mapping[str, Any],
    *,
    reference_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Decode a root document while keeping its field names unambiguous."""
    return {str(key): decode_value(value, reference_factory=reference_factory) for key, value in document.items()}


def decode_stored_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Decode a root document for an internal RMW cycle."""
    return {str(key): decode_value(value, _mark_stored_timestamps=True) for key, value in document.items()}
