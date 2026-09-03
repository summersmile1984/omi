"""A minimal JSON Schema validator, shared by apply.py and check.py.

Every fork guard script in this repo runs on bare Python with no install
step (see scripts/fork/check-upstream-touch.py's own docstring) -- CI's fast
path should not grow a dependency just to validate one manifest shape. This
implements the specific, bounded subset of JSON Schema brand/_schema/manifest.schema.json
actually uses: object/array/string/boolean/null types, required,
additionalProperties, pattern, format (email/uri), enum, const, minLength,
maxLength, minItems, items, oneOf, and same-document $ref/$defs resolution.
It is not a general JSON Schema implementation -- allOf/anyOf/not/if-then-else
and cross-file $ref are deliberately unsupported; a schema needing them
belongs to a different validator, not a silently-partial one here.
"""

from __future__ import annotations

import re
from typing import Any


class SchemaError(Exception):
    """The manifest does not match the schema. Always fatal."""


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URI_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _resolve(schema: dict, root: dict) -> dict:
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            raise SchemaError(f"unsupported $ref (same-document $defs only): {ref}")
        target = root
        for part in ref.split("/")[1:]:
            target = target[part]
        return target
    return schema


def _check_format(value: str, fmt: str, path: str) -> list[str]:
    if fmt == "email" and not _EMAIL_RE.match(value):
        return [f"{path}: {value!r} is not a valid email"]
    if fmt == "uri" and not _URI_RE.match(value):
        return [f"{path}: {value!r} is not a valid URI"]
    return []


def _validate(value: Any, schema: dict, root: dict, path: str) -> list[str]:
    schema = _resolve(schema, root)
    errors: list[str] = []

    if "oneOf" in schema:
        branch_errors = [_validate(value, branch, root, path) for branch in schema["oneOf"]]
        if not any(not errs for errs in branch_errors):
            errors.append(f"{path}: matches none of {len(schema['oneOf'])} allowed shapes")
        return errors

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected exactly {schema['const']!r}, got {value!r}")
        return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
        return errors

    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected an object, got {type(value).__name__}"]
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                errors.append(f"{path}: unknown key(s) {sorted(unknown)}")
        for key, subvalue in value.items():
            if key in properties:
                errors.extend(_validate(subvalue, properties[key], root, f"{path}.{key}"))
    elif expected_type == "array":
        if not isinstance(value, list):
            return [f"{path}: expected an array, got {type(value).__name__}"]
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            errors.append(f"{path}: expected at least {min_items} item(s), got {len(value)}")
        item_schema = schema.get("items")
        if item_schema is not None:
            for i, item in enumerate(value):
                errors.extend(_validate(item, item_schema, root, f"{path}[{i}]"))
    elif expected_type == "string":
        if not isinstance(value, str):
            return [f"{path}: expected a string, got {type(value).__name__}"]
        pattern = schema.get("pattern")
        if pattern is not None and not re.match(pattern, value):
            errors.append(f"{path}: {value!r} does not match pattern {pattern!r}")
        fmt = schema.get("format")
        if fmt is not None:
            errors.extend(_check_format(value, fmt, path))
        min_length = schema.get("minLength")
        if min_length is not None and len(value) < min_length:
            errors.append(f"{path}: shorter than the required {min_length} character(s)")
        max_length = schema.get("maxLength")
        if max_length is not None and len(value) > max_length:
            errors.append(f"{path}: longer than the allowed {max_length} character(s)")
    elif expected_type == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{path}: expected a boolean, got {type(value).__name__}")
    elif expected_type == "null":
        if value is not None:
            errors.append(f"{path}: expected null, got {type(value).__name__}")
    elif expected_type is not None:
        raise SchemaError(f"unsupported schema type at {path}: {expected_type!r}")

    return errors


def validate(manifest: dict, schema: dict) -> list[str]:
    """Return a list of human-readable errors; empty means the manifest is valid."""
    return _validate(manifest, schema, schema, "manifest")
