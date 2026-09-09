#!/usr/bin/env python3
"""Build and Developer ID sign an isolated fork app; never install or launch it.

Signing and native dependency closure are artifact evidence. Remote service
acceptance and Apple notarization remain separately recorded verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import shutil
import struct
import subprocess
from pathlib import Path

from build import build, install_brand_package_resources, local_info_plist


def runtime_code(path: Path) -> bool:
    """Fat static archives share Mach-O's outer magic, but are not runtime code."""
    with path.open("rb") as stream:
        header = stream.read(8)
        if len(header) < 8:
            return False
        magic = header[:4]
        offsets = [0]
        if magic in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
            count = struct.unpack(">I", header[4:])[0]
            if not 1 <= count <= 64:
                raise ValueError("Invalid universal binary architecture table")
            wide = magic == b"\xca\xfe\xba\xbf"
            offsets = []
            for _ in range(count):
                entry = stream.read(32 if wide else 20)
                offsets.append(struct.unpack(">Q" if wide else ">I", entry[8:16] if wide else entry[8:12])[0])
        kinds = []
        for offset in offsets:
            stream.seek(offset)
            header = stream.read(16)
            kinds.append(
                len(header) == 16
                and header[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe")
                and struct.unpack("<I", header[12:16])[0] in (2, 6, 8)
            )
        if any(kinds) and not all(kinds):
            raise ValueError("Universal artifact mixes runtime code and unsupported slices")
        return all(kinds)


def command(*args: str) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=180)
    return result.stdout + result.stderr


def macho_files(app: Path) -> list[Path]:
    result = []
    for path in app.rglob("*"):
        if path.is_file() and not path.is_symlink():
            if runtime_code(path):
                result.append(path)
    return result


def dependencies(path: Path) -> list[str]:
    return [
        line.strip().split(" (compatibility version", 1)[0]
        for line in command("otool", "-L", str(path)).splitlines()
        if " (compatibility version" in line
    ]


def normalize_resource_bundle(bundle: Path) -> None:
    """Use one macOS resource directory for AppKit and the packaged Node path."""
    resources = bundle / "Contents/Resources"
    resources.mkdir(parents=True, exist_ok=True)
    for path in list(bundle.iterdir()):
        if path.name != "Contents":
            target = resources / path.name
            if target.exists() or target.is_symlink():
                raise ValueError("Resource normalization would overwrite a packaged entry")
            path.rename(target)


def vendor_dependencies(app: Path) -> dict:
    """Resolve actual load commands and vendor non-system dylibs by content."""
    frameworks = app / "Contents/Frameworks"
    seen = set()
    vendored = {}
    while pending := [path for path in macho_files(app) if path not in seen]:
        for path in pending:
            seen.add(path)
            path.chmod(path.stat().st_mode | 0o200)
            for dependency in dependencies(path):
                if dependency.startswith(("/System/Library/", "/usr/lib/")):
                    continue
                if dependency.startswith("/"):
                    original = Path(dependency).resolve(strict=True)
                elif dependency.startswith(("@rpath/", "@loader_path/")) and path.name in vendored:
                    name = dependency.split("/", 1)[1]
                    if (frameworks / name).exists() or name == path.name:
                        continue
                    # A vendored Homebrew dylib may name a sibling through
                    # @rpath and a version symlink. Resolve it from that exact
                    # source owner, then rewrite to the copied real filename.
                    original = (Path(vendored[path.name]["source"]).parent / name).resolve(strict=True)
                else:
                    continue
                if app in original.parents:
                    continue
                if original.suffix != ".dylib":
                    raise ValueError("Non-portable native dependency requires an explicit bundle owner: " + dependency)
                content = hashlib.sha256(original.read_bytes()).hexdigest()
                if original.name in vendored and vendored[original.name]["sha256"] != content:
                    raise ValueError("Native dependency basename has conflicting content")
                target = frameworks / original.name
                if original.name not in vendored:
                    if target.exists():
                        raise ValueError("Native dependency collides with an existing packaged library")
                    shutil.copy2(original, target)
                    target.chmod(target.stat().st_mode | 0o200)
                    command("install_name_tool", "-id", "@rpath/" + target.name, str(target))
                    vendored[target.name] = {"source": str(original), "sha256": content}
                command("install_name_tool", "-change", dependency, "@rpath/" + target.name, str(path))
            # SwiftPM includes its build-directory rpath. No host path belongs
            # in the distributed loader search, even when the first path works.
            load_commands = command("otool", "-l", str(path))
            for match in re.finditer(r"cmd LC_RPATH\n.*?\n\s*path (.+?) \(offset", load_commands):
                rpath = match[1]
                if rpath.startswith("/") and not rpath.startswith(("/System/Library/", "/usr/lib/")):
                    command("install_name_tool", "-delete_rpath", rpath, str(path))
    return vendored


def signing_details(output: str, team: str, bundle_id: str | None = None) -> None:
    if f"TeamIdentifier={team}" not in output.splitlines():
        raise ValueError("Actual code signature does not match the manifest signing team")
    if not any(line.startswith("Authority=Developer ID Application:") for line in output.splitlines()):
        raise ValueError("A Developer ID Application signature is required")
    if "runtime" not in output or not any(line.startswith("Timestamp=") for line in output.splitlines()):
        raise ValueError("A timestamped hardened runtime signature is required")
    if bundle_id and f"Identifier={bundle_id}" not in output.splitlines():
        raise ValueError("Signed bundle identifier differs from the staged identity")


def sign_app(app: Path, identity: str, team: str, entitlements: Path, bundle_id: str) -> None:
    if not re.fullmatch(r"[A-Fa-f0-9]{40}", identity):
        raise ValueError("Select the exact SHA-1 of a Developer ID Application identity")
    code = macho_files(app)
    nested = [
        path
        for path in app.rglob("*")
        if path.is_dir() and not path.is_symlink() and path.suffix in (".framework", ".app", ".xpc")
    ]
    main = app / "Contents/MacOS" / plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleExecutable"]
    node_entitlements = entitlements.parent / "ForkNode.entitlements"
    node_entitlements.write_bytes(
        plistlib.dumps(
            {"com.apple.security.cs.allow-jit": True, "com.apple.security.cs.allow-unsigned-executable-memory": True}
        )
    )
    for path in sorted(set(code + nested) - {main}, key=lambda p: len(p.parts), reverse=True):
        options = ["--entitlements", str(node_entitlements)] if path.name == "node" else []
        command("codesign", "--force", "--options", "runtime", "--timestamp", *options, "--sign", identity, str(path))
    command(
        "codesign",
        "--force",
        "--options",
        "runtime",
        "--timestamp",
        "--entitlements",
        str(entitlements),
        "--sign",
        identity,
        str(app),
    )
    command("codesign", "--verify", "--deep", "--strict", str(app))
    signing_details(command("codesign", "--display", "--verbose=4", str(app)), team, bundle_id)


def package(output: Path, binary: Path, identity: str, version: str, build_number: str) -> Path:
    proof = json.loads((output / "stage-manifest.json").read_text())
    if proof["distribution"] not in ("beta", "production") or not proof["profile"].endswith(
        "." + proof["distribution"]
    ):
        raise ValueError("Developer ID distribution requires a matching staged identity and profile")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", version) or not re.fullmatch(r"[0-9]+", build_number):
        raise ValueError("Explicit numeric product version and build number are required")
    products = binary.parent
    app = output / (proof["app_name"] + ".app")
    contents = app / "Contents"
    resources, frameworks = contents / "Resources", contents / "Frameworks"
    executable = contents / "MacOS" / proof["app_name"]
    if app.exists():
        raise ValueError("Distribution output must be new")
    for path in (resources, frameworks, executable.parent):
        path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, executable)
    command("install_name_tool", "-add_rpath", "@executable_path/../Frameworks", str(executable))
    for path in products.glob("*.framework"):
        info = plistlib.loads((path / "Resources/Info.plist").read_bytes())
        if runtime_code(path / info["CFBundleExecutable"]):
            shutil.copytree(path, frameworks / path.name, symlinks=True)
    for path in products.glob("*.bundle"):
        shutil.copytree(path, resources / path.name, symlinks=True)
    assets = install_brand_package_resources(output, resources, proof)
    shutil.copy2(output / "ForkDeployment.json", resources / "ForkDeployment.json")
    resource_bundle = resources / "Omi Computer_Omi Computer.bundle"
    normalize_resource_bundle(resource_bundle)
    assets = {
        name.replace("Omi Computer_Omi Computer.bundle/", "Omi Computer_Omi Computer.bundle/Contents/Resources/"): value
        for name, value in assets.items()
    }
    for name, modules in [("agent", "agent-node_modules"), ("pi-mono-extension", "pi-mono-extension-node_modules")]:
        destination = resources / name
        destination.mkdir()
        shutil.copy2(output / name / "package.json", destination / "package.json")
        shutil.copytree(output / ".harness/agent-runtime" / modules, destination / "node_modules", symlinks=True)
    shutil.copytree(output / "agent/dist", resources / "agent/dist", symlinks=True)
    for name in ("index.ts", "package-lock.json"):
        shutil.copy2(output / "pi-mono-extension" / name, resources / "pi-mono-extension" / name)
    plist = plistlib.loads(local_info_plist(proof, (output / "Desktop/Info.plist").read_bytes()))
    plist.update(
        CFBundleShortVersionString=version,
        CFBundleVersion=build_number,
        NSAppTransportSecurity={"NSAllowsArbitraryLoads": False},
    )
    for key, value in plist.items():
        if key.endswith("UsageDescription") and isinstance(value, str):
            plist[key] = value.replace("Omi", proof["product_name"])
    (contents / "Info.plist").write_bytes(plistlib.dumps(plist))
    command("bash", str(output / "scripts/prepare-desktop-bundle-native-deps.sh"), str(app))
    vendored = vendor_dependencies(app)
    floors = ["14.0"]
    for path in macho_files(app):
        floors.extend(re.findall(r"\bminos\s+([0-9.]+)", command("xcrun", "vtool", "-show-build", str(path))))
    minimum_os = max(floors, key=lambda v: tuple(int(part) for part in v.split(".")))
    plist["LSMinimumSystemVersion"] = minimum_os
    (contents / "Info.plist").write_bytes(plistlib.dumps(plist))
    entitlements = output / "ForkRelease.entitlements"
    entitlements.write_bytes(
        plistlib.dumps(
            {
                "com.apple.security.automation.apple-events": True,
                "com.apple.security.device.audio-input": True,
                "com.apple.security.device.screen-capture": True,
            }
        )
    )
    sign_app(app, identity, proof["signing_team"], entitlements, proof["bundle_id"])
    # Use the existing repository dependency audit; it exercises the complete
    # assembled Mach-O graph and verifies signatures, including linked rpaths.
    audit = command("bash", str(Path(__file__).parent.parent / "scripts/audit-desktop-bundle-deps.sh"), str(app))
    (output / "dependency-audit.txt").write_text(audit)
    proof.update(
        app=str(app),
        signing="Developer ID Application",
        minimum_macos=minimum_os,
        version=version,
        build_number=build_number,
        packaged_assets=assets,
        vendored_dependencies=vendored,
        notarized=False,
        service_verified=False,
        qualification="signed-native-artifact",
        release_ready=False,
    )
    (output / "artifact-manifest.json").write_text(json.dumps(proof, indent=2) + "\n")
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--target", choices=("cloudflare", "self_hosted"), required=True)
    parser.add_argument("--stage", choices=("beta", "production"), required=True)
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependency-cache", type=Path)
    parser.add_argument("--signing-identity", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    binary = build(
        args.manifest,
        args.target,
        args.app_name,
        args.output,
        args.dependency_cache,
        compile_only=True,
        deployment_stage=args.stage,
        distribution=args.stage,
        configuration="release",
    )
    print(package(args.output.resolve(), binary, args.signing_identity, args.version, args.build_number))
