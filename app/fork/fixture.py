"""Disposable brand inputs; never writes a formal brand or supplies credentials."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/brand"))
from yaml_lite import load_yaml  # noqa: E402


def fixture(directory: Path, variant: str = "harbor"):
    manifest = deepcopy(load_yaml(ROOT / "brand/omi-upstream/manifest.yaml"))
    manifest["brand"].update(id="flutter-proof", display_name="Native Proof", short_name="Native Proof")
    manifest["identifiers"].update(
        android_application_id="invalid.example.nativeproof",
        android_application_id_dev="invalid.example.nativeproof.dev",
    )
    manifest["domains"] = {key: f"https://{key.replace('_', '-')}.example.invalid" for key in manifest["domains"]}
    manifest["domains"].update(
        auth_base="https://auth.example.invalid",
        mcp_base="https://mcp.example.invalid",
        objects_base="https://objects.example.invalid",
    )
    manifest["deployments"] = {}
    for target, auth, api in [("self_hosted", 33067, 33068), ("cloudflare", 33058, 33062)]:
        manifest["deployments"][target] = {
            "local": {
                "api_base": f"http://127.0.0.1:{api}",
                "auth_base": f"http://127.0.0.1:{auth}",
                "web_app": "http://127.0.0.1:33059",
                "mcp_base": f"http://127.0.0.1:{api}",
                "objects_base": f"http://127.0.0.1:{api}",
                "share_base": f"http://127.0.0.1:{api}",
            }
        }
    subprocess.run(["node", str(ROOT / "scripts/brand/raster/fixture.mjs"), str(directory), variant], check=True)
    manifest["assets"].update(
        {name: f"assets/{variant}-{name}.png" for name in ("icon_master", "logo_light", "logo_dark", "splash")}
    )
    return manifest


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit("usage: fixture.py <temporary-directory> [variant]")
    print(json.dumps(fixture(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) == 3 else "harbor")))
