"""The brand manifest boundary shared by generation, scanning and profiles."""

from __future__ import annotations

import json
import re
from pathlib import Path

from schema_validate import validate
from yaml_lite import YamlError, load_yaml


class ManifestError(RuntimeError):
    pass


def load_manifest(brand_id: str | None, repo_root: Path, manifest_path: Path | None = None) -> dict:
    """Load a checked-in brand or an explicit private/fixture overlay.

    An explicit path need not live in brand/<id>, but --brand, when supplied,
    must match its identity. No caller can resolve an unvalidated manifest.
    """
    if brand_id is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", brand_id):
        raise ManifestError("invalid brand directory id")
    path = manifest_path or repo_root / "brand" / (brand_id or "omi-upstream") / "manifest.yaml"
    try:
        if not path.is_file():
            raise ManifestError(f"no manifest at {path}")
        source = path.read_text(encoding="utf-8")
        manifest = json.loads(source) if source.lstrip().startswith("{") else load_yaml(path)
        schema = json.loads((repo_root / "brand/_schema/manifest.schema.json").read_text(encoding="utf-8"))
        errors = validate(manifest, schema)
    except (OSError, ValueError, YamlError) as error:
        raise ManifestError(f"cannot load manifest {path}: {error}") from error
    if errors:
        raise ManifestError(f"{path} does not match the schema:\n" + "\n".join(f"  - {e}" for e in errors))
    expected_id = brand_id if manifest_path else path.parent.name
    if expected_id and manifest["brand"]["id"] != expected_id:
        raise ManifestError(f"brand.id must match requested identity {expected_id!r}")
    return manifest
