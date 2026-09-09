#!/usr/bin/env python3
"""Compile the entire staged native app from a fresh synthetic CI identity.

Network may populate SwiftPM locked dependencies in the CI setup/build phase.
No service, signing identity, personal cache, GUI session or secret is required.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from build import build
from prepare import ROOT, load_manifest

TARGETS = ("self_hosted", "cloudflare")


def synthetic_manifest(directory: Path, variant: str = "harbor") -> dict:
    value = copy.deepcopy(load_manifest("omi-upstream", ROOT))
    value["brand"].update(id="synthetic-native-ci", display_name="Synthetic Native CI")
    value["identifiers"].update(
        macos_bundle_id="test.synthetic.native",
        macos_bundle_id_beta="test.synthetic.native.beta",
        macos_bundle_id_dev="test.synthetic.native.dev",
        macos_named_bundle_prefix="test.synthetic.",
        keychain_service_prefix="test.synthetic.",
    )
    # Compile-only input, never running or connecting to these local origins.
    row = {
        key: "http://127.0.0.1:39901"
        for key in ("api_base", "auth_base", "web_app", "mcp_base", "share_base", "objects_base")
    }
    value["deployments"] = {target: {"local": row} for target in ("self_hosted", "cloudflare")}
    subprocess.run(["node", str(ROOT / "scripts/brand/raster/fixture.mjs"), str(directory), variant], check=True)
    value["assets"].update(
        {name: f"assets/{variant}-{name}.png" for name in ("icon_master", "logo_light", "logo_dark", "splash")}
    )
    return value


def build_matrix(output: Path, dependency_cache: Path | None = None) -> dict[str, Path]:
    """Compile each local target from the one synthetic private brand input."""

    output = output.resolve()
    if output.exists() or output.is_symlink() or output == ROOT or ROOT in output.parents:
        raise ValueError("matrix output must be a fresh directory outside the source repository")
    with tempfile.TemporaryDirectory(prefix="native-ci-manifest-") as temporary:
        manifest = Path(temporary) / "brand.json"
        manifest.write_text(json.dumps(synthetic_manifest(Path(temporary))))
        results = {}
        try:
            for target in TARGETS:
                results[target] = build(
                    manifest,
                    target,
                    f"omi-native-ci-{target.replace('_', '-')}",
                    output / target,
                    dependency_cache,
                    compile_only=True,
                )
        except BaseException:
            shutil.rmtree(output, ignore_errors=True)
            raise
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependency-cache", type=Path)
    args = parser.parse_args()
    print(json.dumps({target: str(path) for target, path in build_matrix(args.output, args.dependency_cache).items()}))
