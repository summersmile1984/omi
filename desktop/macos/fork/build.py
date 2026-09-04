#!/usr/bin/env python3
"""Build an isolated, ad-hoc signed local macOS app; never install or launch it."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from prepare import ROOT, stage


def run(*arguments: str) -> None:
    environment = {**os.environ, "PATH": "/opt/homebrew/bin:" + os.environ.get("PATH", "")}
    subprocess.run(arguments, check=True, env=environment)


def package_local(output: Path) -> Path:
    proof = json.loads((output / "stage-manifest.json").read_text())
    if not proof["profile"].endswith(".local") or proof["release_ready"] or not proof["app_name"].startswith("omi-"):
        raise ValueError("Only a named local stage can be packaged")
    products = output / "Desktop/.build/debug"
    app = output / f'{proof["app_name"]}.app'
    if app.exists():
        raise ValueError("The named app output already exists")
    contents = app / "Contents"
    resources = contents / "Resources"
    frameworks = contents / "Frameworks"
    executable = contents / "MacOS" / proof["app_name"]
    for path in (resources, frameworks, executable.parent):
        path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(products / "Omi Computer", executable)
    run("install_name_tool", "-add_rpath", "@executable_path/../Frameworks", str(executable))
    for path in products.glob("*.framework"):
        shutil.copytree(path, frameworks / path.name, symlinks=True)
    for path in products.glob("*.bundle"):
        shutil.copytree(path, resources / path.name, symlinks=True)
    # This is an explicitly local development artifact, not a portable release.
    # Vendor/toolchain deployment-floor qualification belongs to distribution.
    shutil.copy2(output / "ForkDeployment.json", resources / "ForkDeployment.json")
    plist = plistlib.loads((output / "Desktop/Info.plist").read_bytes())
    plist.update(
        CFBundleIdentifier=proof["bundle_id"],
        CFBundleExecutable=proof["app_name"],
        CFBundleName=proof["app_name"],
        CFBundleDisplayName=proof["app_name"],
        CFBundleURLTypes=[],
        SUEnableAutomaticChecks=False,
        SUAutomaticallyUpdate=False,
        ForkDeploymentProfile=proof["profile"],
    )
    for key in ("SUFeedURL", "SUPublicEDKey", "OMIExternalPreview", "OMIExternalPreviewBackend"):
        plist.pop(key, None)
    (contents / "Info.plist").write_bytes(plistlib.dumps(plist))
    run("codesign", "--force", "--deep", "--sign", "-", str(app))
    run("codesign", "--verify", "--deep", "--strict", str(app))
    proof.update(app=str(app), signing="ad-hoc", qualification="local-native-auth-only")
    (output / "artifact-manifest.json").write_text(json.dumps(proof, indent=2) + "\n")
    return app


def build(
    manifest: Path,
    target: str,
    app_name: str,
    output: Path,
    dependency_cache: Path | None = None,
    compile_only: bool = False,
) -> Path:
    output = output.resolve()
    stage(manifest, target, app_name, output)
    if dependency_cache:
        # Copy checkout/artifact repositories only. Compiled module caches encode
        # absolute source/cache paths and cannot be reused at a new stage path.
        cache = output / "Desktop/.build"
        cache.mkdir()
        for name in ("artifacts", "checkouts", "repositories", "workspace-state.json"):
            source = dependency_cache.resolve(strict=True) / name
            if source.is_dir():
                shutil.copytree(source, cache / name, symlinks=True)
            elif source.is_file():
                shutil.copy2(source, cache / name)
    run(
        "xcrun",
        "swift",
        "build",
        "-c",
        "debug",
        "--package-path",
        str(output / "Desktop"),
        "--disable-automatic-resolution",
    )
    if compile_only:
        return output / "Desktop/.build/debug/Omi Computer"
    return package_local(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependency-cache", type=Path)
    args = parser.parse_args()
    print(build(args.manifest, args.target, args.app_name, args.output, args.dependency_cache))
