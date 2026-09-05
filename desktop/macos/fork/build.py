#!/usr/bin/env python3
"""Build an isolated, ad-hoc signed local macOS app; never install or launch it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from prepare import ROOT, stage

RESOURCE_BUNDLE_NAME = "Omi Computer_Omi Computer.bundle"
BRAND_RESOURCE_NAMES = (
    "omi_app_icon.png",
    "omi_menu_bar_icon.png",
    "herologo.png",
    "ForkBrandLight.png",
    "ForkBrandDark.png",
)


def run(*arguments: str) -> None:
    environment = {**os.environ, "PATH": "/opt/homebrew/bin:" + os.environ.get("PATH", "")}
    subprocess.run(arguments, check=True, env=environment)


def install_brand_package_resources(output: Path, resources: Path, proof: dict) -> dict:
    """Verify SwiftPM's copied resource bundle and install the selected Finder icon."""
    bundle = resources / RESOURCE_BUNDLE_NAME
    assets = proof["assets"]["outputs"]
    installed = {}
    for name in BRAND_RESOURCE_NAMES:
        key = f"Desktop/Sources/Resources/{name}"
        source = output / key
        packaged = bundle / name
        expected = assets[key]
        source_bytes = source.read_bytes()
        packaged_bytes = packaged.read_bytes()
        if source_bytes != packaged_bytes or hashlib.sha256(packaged_bytes).hexdigest() != expected["sha256"]:
            raise ValueError(f"Packaged brand resource differs from the selected stage: {name}")
        installed[f"{RESOURCE_BUNDLE_NAME}/{name}"] = expected
    icon = output / "ForkAppIcon.icns"
    icon_bytes = icon.read_bytes()
    expected_icon = assets["ForkAppIcon.icns"]
    if hashlib.sha256(icon_bytes).hexdigest() != expected_icon["sha256"]:
        raise ValueError("Generated application icon differs from the selected stage")
    shutil.copy2(icon, resources / "ForkAppIcon.icns")
    installed["ForkAppIcon.icns"] = expected_icon
    return installed


def local_info_plist(proof: dict, original: bytes) -> bytes:
    plist = plistlib.loads(original)
    plist.update(
        CFBundleIdentifier=proof["bundle_id"],
        CFBundleExecutable=proof["app_name"],
        CFBundleName=proof["app_name"],
        CFBundleDisplayName=proof["product_name"],
        CFBundleIconFile="ForkAppIcon",
        CFBundleURLTypes=[],
        SUEnableAutomaticChecks=False,
        SUAutomaticallyUpdate=False,
        ForkDeploymentProfile=proof["profile"],
    )
    for key in ("SUFeedURL", "SUPublicEDKey", "OMIExternalPreview", "OMIExternalPreviewBackend"):
        plist.pop(key, None)
    return plistlib.dumps(plist)


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
    packaged_assets = install_brand_package_resources(output, resources, proof)
    # This is an explicitly local development artifact, not a portable release.
    # Vendor/toolchain deployment-floor qualification belongs to distribution.
    shutil.copy2(output / "ForkDeployment.json", resources / "ForkDeployment.json")
    (contents / "Info.plist").write_bytes(local_info_plist(proof, (output / "Desktop/Info.plist").read_bytes()))
    run("codesign", "--force", "--deep", "--sign", "-", str(app))
    run("codesign", "--verify", "--deep", "--strict", str(app))
    proof.update(
        app=str(app),
        signing="ad-hoc",
        qualification="local-native-auth-and-brand-assets-only",
        packaged_assets=packaged_assets,
    )
    (output / "artifact-manifest.json").write_text(json.dumps(proof, indent=2) + "\n")
    return app


def build(
    manifest: Path,
    target: str,
    app_name: str,
    output: Path,
    dependency_cache: Path | None = None,
    compile_only: bool = False,
    *,
    deployment_stage: str = "local",
    distribution: str = "development",
    configuration: str = "debug",
) -> Path:
    if configuration not in ("debug", "release"):
        raise ValueError("Select debug or release compilation")
    output = output.resolve()
    stage(manifest, target, app_name, output, deployment_stage=deployment_stage, distribution=distribution)
    if distribution != "development":
        for name in ("scripts", "agent", "pi-mono-extension"):
            shutil.copytree(
                ROOT / "desktop/macos" / name,
                output / name,
                ignore=shutil.ignore_patterns("node_modules", "dist", ".DS_Store"),
                symlinks=True,
            )
        run("bash", str(output / "scripts/prepare-agent-runtime.sh"), "--universal-node")
    if dependency_cache:
        # Copy checkout/artifact repositories only. Compiled module caches encode
        # absolute source/cache paths and cannot be reused at a new stage path.
        cache = output / "Desktop/.build"
        cache.mkdir()
        for name in ("artifacts", "checkouts", "repositories", "workspace-state.json"):
            source = dependency_cache.resolve(strict=True) / name
            if source.is_dir():
                # APFS clones preserve separate writable ownership without
                # duplicating multi-gigabyte locked SwiftPM downloads.
                run("/bin/cp", "-cR", str(source), str(cache / name))
            elif source.is_file():
                shutil.copy2(source, cache / name)
    run(
        "xcrun",
        "swift",
        "build",
        "-c",
        configuration,
        "--package-path",
        str(output / "Desktop"),
        "--disable-automatic-resolution",
    )
    if compile_only:
        return output / f"Desktop/.build/{configuration}/Omi Computer"
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
