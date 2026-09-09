"""Execute the profile renderer/checker against temporary branded build trees.

Audit E2 showed a schema-valid manifest produced an empty objects URL and
silently selected API as auth. Both target implementations share these tests.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render

ROOT = Path(__file__).resolve().parents[2]
FIELDS = ("api_base", "auth_base", "web_app", "mcp_base", "share_base", "objects_base")


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = render.load_manifest("omi-upstream", ROOT)
        self.manifest["brand"].update(id="fixture-weft", display_name="Weft")
        self.manifest["identifiers"]["url_scheme"] = "weft"
        self.path = self.root / "manifest.json"

    def write(self):
        self.path.write_text(json.dumps(self.manifest))

    def endpoints(self, label):
        return {field: f"https://{field.replace('_', '-')}.{label}.example" for field in FIELDS}

    def configure(self):
        self.manifest["deployments"] = {
            target: {stage: self.endpoints(f"{target.replace('_','-')}-{stage}") for stage in render.STAGES}
            for target in ("self_hosted", "cloudflare")
        }
        self.write()

    def cli(self, target, *args):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/profiles/render.py"),
                "--target",
                target,
                "--manifest",
                str(self.path),
                *args,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_two_targets_three_stages_preserve_resolved_contract_with_distinct_origins(self):
        self.configure()
        for target in ("self_hosted", "cloudflare"):
            proc = self.cli(target, "--emit-json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(set(data), {"schema_version", "target", "brand", "profiles"})
            self.assertEqual(len(data["profiles"]), 3)
            for stage in render.STAGES:
                row = data["profiles"][f"{target}.{stage}"]
                origins = self.manifest["deployments"][target][stage]
                self.assertEqual(row["identity_provider"], "better_auth")
                self.assertEqual(row["auth_base_url"], origins["auth_base"])
                self.assertEqual(row["objects_base_url"], origins["objects_base"])
                self.assertEqual(row["data_plane"]["store"], "d1" if target == "cloudflare" else "firestore_pg")
                self.assertFalse(row["managed"])
        self.assertFalse((ROOT / "brand/fixture-weft").exists())

    def test_selfhost_model_identity_is_the_capability_dimension_owner(self):
        self.configure()
        table = json.loads(self.cli("self_hosted", "--emit-json").stdout)
        row = table["profiles"]["self_hosted.production"]
        self.assertEqual(row["embedding"]["dimension"], row["capabilities"]["embedding_dims"])
        self.assertEqual(row["embedding"]["dimension"], 1024)
        self.assertTrue(row["embedding"]["manifest_digest"].startswith("sha256:"))
        self.assertTrue(row["embedding"]["artifact_digest"].startswith("sha256:"))
        self.assertEqual(row["capabilities"]["stt_providers"], ["sensevoice"])
        self.assertEqual(row["capabilities"]["tts_provider"], "kokoro")
        self.assertTrue(row["speech"]["bundle_digest"].startswith("sha256:"))
        self.assertEqual(row["capabilities"]["push_provider"], "disabled")
        self.assertEqual(row["capabilities"]["llm_provider"], row["llm"]["provider"])
        self.assertEqual(row["llm"]["context_length"], 40960)
        self.assertEqual(row["llm"]["context_window"], 8192)
        self.assertEqual(row["llm"]["max_output_tokens"], 2048)
        self.assertEqual(row["llm"]["kv_cache_type"], "q8_0")
        self.assertEqual(row["llm"]["cpu_threads"], 4)
        self.assertEqual(row["llm"]["parallel_requests"], 1)
        self.assertEqual(row["llm"]["request_timeout_seconds"], 300)

    def test_mimo_selection_is_explicit_local_only_and_leaves_embedding_unchanged(self):
        self.configure()
        baseline = json.loads(self.cli('self_hosted', '--stage', 'local', '--emit-json').stdout)
        selected = self.cli('self_hosted', '--stage', 'local', '--operator-ai', 'mimo-cn', '--emit-json')
        self.assertEqual(selected.returncode, 0, selected.stderr)
        row = json.loads(selected.stdout)['profiles']['self_hosted.local']
        self.assertEqual(row['embedding'], baseline['profiles']['self_hosted.local']['embedding'])
        self.assertEqual(row['capabilities']['llm_provider'], 'mimo')
        self.assertEqual(row['operator_ai']['asr_model'], 'mimo-v2.5-asr')
        self.assertNotIn('llm', row)
        self.assertNotIn('speech', row)
        for target, stage in [('cloudflare', 'local'), ('self_hosted', 'production')]:
            denied = self.cli(target, '--stage', stage, '--operator-ai', 'mimo-cn', '--emit-json')
            self.assertNotEqual(denied.returncode, 0)

    def test_all_five_generated_outputs_are_checked_and_missing_is_failure(self):
        self.configure()
        for target in ("self_hosted", "cloudflare"):
            output = self.root / target
            proc = self.cli(target, "--output-root", str(output))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len([p for p in output.rglob('*') if p.is_file()]), 5)
            args = [
                sys.executable,
                str(ROOT / "scripts/profiles/check_tables.py"),
                "--target",
                target,
                "--manifest",
                str(self.path),
                "--output-root",
                str(output),
            ]
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 0)
            missing = output / "web/app/src/lib/fork/deploymentProfile.generated.ts"
            missing.unlink()
            proc = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(missing.exists())

    def test_missing_fork_endpoints_fail_instead_of_falling_back(self):
        self.write()
        for target in ("self_hosted", "cloudflare"):
            proc = self.cli(target, "--emit-json")
            self.assertEqual(proc.returncode, 1)
            self.assertIn("missing explicit endpoint auth_base", proc.stderr)
        self.manifest["domains"].update(auth_base="https://auth.example", mcp_base="https://mcp.example")
        self.write()
        self.assertIn("missing explicit endpoint objects_base", self.cli("self_hosted", "--emit-json").stderr)

    def test_shared_origins_are_explicit_and_supported_across_targets(self):
        self.manifest["domains"].update(self.endpoints("shared"))
        self.write()
        for target in ("self_hosted", "cloudflare"):
            row = json.loads(self.cli(target, "--emit-json").stdout)["profiles"][f"{target}.production"]
            self.assertEqual(row["auth_base_url"], "https://auth-base.shared.example")

    def test_eddy_resolves_separate_server_and_cloudflare_public_origins(self):
        # Exercise the shipped brand, not a fixture that already supplies the
        # missing overrides: Eddy previously sent both targets to CF Workers.
        for stage, suffix in (("production", ""), ("beta", "-beta")):
            for target, label in (("cloudflare", "cf"), ("self_hosted", "server")):
                with self.subTest(target=target, stage=stage):
                    row = render.resolve(target, "eddy", stage=stage)["profiles"][f"{target}.{stage}"]
                    prefix = f"eddy-{label}{suffix}"
                    api = f"https://{prefix}-api.smartipproxy.com"
                    web = f"https://{prefix}.smartipproxy.com"
                    self.assertEqual(row["api_base_url"], api)
                    self.assertEqual(row["auth_base_url"], f"https://{prefix}-auth.smartipproxy.com")
                    self.assertEqual(row["mcp_base_url"], api)
                    self.assertEqual(row["web_base_url"], web)
                    self.assertEqual(row["share_base_url"], web)
                    self.assertEqual(
                        row["objects_base_url"],
                        api if target == "cloudflare" else f"https://{prefix}-objects.smartipproxy.com",
                    )
        for target, port in (("cloudflare", 8787), ("self_hosted", 8100)):
            row = render.resolve(target, "eddy", stage="local")["profiles"][f"{target}.local"]
            self.assertEqual(row["api_base_url"], f"http://127.0.0.1:{port}/")

    def test_local_only_resolution_needs_no_fictional_production_endpoints(self):
        self.write()
        for target, port in (("self_hosted", 8100), ("cloudflare", 8787)):
            proc = self.cli(target, "--stage", "local", "--emit-json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = json.loads(proc.stdout)["profiles"]
            self.assertEqual(list(rows), [f"{target}.local"])
            self.assertEqual(rows[f"{target}.local"]["api_base_url"], f"http://127.0.0.1:{port}/")

    def test_invalid_topology_is_rejected_by_shared_schema_and_resolver(self):
        self.configure()
        for value in ("", "http://auth.example", "https://user:password@auth.example", "https://auth.example?q=x"):
            with self.subTest(value=value):
                self.manifest["deployments"]["cloudflare"]["production"]["auth_base"] = value
                self.write()
                self.assertEqual(self.cli("cloudflare", "--emit-json").returncode, 1)
        self.configure()
        self.manifest["deployments"]["cloudflare"]["production"]["typo_base"] = "https://typo.example"
        self.write()
        self.assertIn("unknown key", self.cli("cloudflare", "--emit-json").stderr)

    def test_legacy_upstream_principal_and_generated_bytes_are_unchanged(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/profiles/check_tables.py")], cwd=ROOT, capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = render.resolve("omi_cloud", "omi-upstream")
        self.assertEqual(data["profiles"]["omi_cloud.production"]["firebase_project_id"], "based-hardware")
        self.assertIn("local_prod", data["profiles"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
