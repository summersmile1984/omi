"""Behavioral tests for the brand manifest tooling (schema_validate, apply, check).

Each test either exercises the real production module against a throwaway
manifest/repo fixture, or runs apply.py/check.py as a subprocess against a
throwaway repo -- the same "exercise the real path, not source text" approach
scripts/fork/test_check_upstream_touch.py uses for the zero-touch guard.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BRAND_SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = BRAND_SCRIPTS.parents[1]

sys.path.insert(0, str(BRAND_SCRIPTS))
from schema_validate import validate  # noqa: E402
from yaml_lite import YamlError, load_yaml  # noqa: E402
from generators import mobile  # noqa: E402

MINIMAL_SCHEMA = {
    "type": "object",
    "required": ["name", "count"],
    "additionalProperties": False,
    "$defs": {"positive": {"type": "string", "pattern": "^[a-z]+$"}},
    "properties": {
        "name": {"$ref": "#/$defs/positive"},
        "count": {"type": "string", "enum": ["one", "many"]},
        "tags": {"type": "array", "minItems": 1, "items": {"type": "string"}},
    },
}


class YamlLiteTests(unittest.TestCase):
    def write(self, tmp: Path, text: str) -> Path:
        p = tmp / "doc.yaml"
        p.write_text(text)
        return p

    def fixture_dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_nested_mappings_by_indentation(self):
        doc = self.write(self.fixture_dir(), "a:\n  b:\n    c: 1\n  d: 2\n")
        self.assertEqual(load_yaml(doc), {"a": {"b": {"c": 1}, "d": 2}})

    def test_block_list_under_a_key_is_a_list_not_a_dict(self):
        # The exact ambiguity this reader has to resolve by lookahead: "key:"
        # with nothing after it could start a nested mapping OR a block list.
        doc = self.write(self.fixture_dir(), "words:\n  - Omi\n  - Friend\n")
        self.assertEqual(load_yaml(doc), {"words": ["Omi", "Friend"]})

    def test_empty_key_with_no_following_list_item_is_a_mapping(self):
        doc = self.write(self.fixture_dir(), "a:\n  b: 1\n")
        self.assertEqual(load_yaml(doc), {"a": {"b": 1}})

    def test_quoted_keys_are_unquoted_but_not_type_coerced(self):
        doc = self.write(self.fixture_dir(), '"true":\n  x: 1\n')
        result = load_yaml(doc)
        self.assertIn("true", result)  # the string "true", not the boolean True
        self.assertNotIn(True, result)

    def test_inline_flow_list_and_quoted_values(self):
        doc = self.write(self.fixture_dir(), 'a: ["x", "y"]\nb: "quoted # not a comment"\n')
        self.assertEqual(load_yaml(doc), {"a": ["x", "y"], "b": "quoted # not a comment"})

    def test_trailing_comment_is_stripped_from_an_unquoted_value(self):
        doc = self.write(self.fixture_dir(), "a: value   # trailing comment\n")
        self.assertEqual(load_yaml(doc), {"a": "value"})

    def test_bool_and_null_scalars(self):
        doc = self.write(self.fixture_dir(), "a: true\nb: false\nc: null\nd: ~\n")
        self.assertEqual(load_yaml(doc), {"a": True, "b": False, "c": None, "d": None})

    def test_list_item_outside_a_list_key_raises(self):
        doc = self.write(self.fixture_dir(), "a: 1\n- b\n")
        with self.assertRaises(YamlError):
            load_yaml(doc)

    def test_missing_file_raises(self):
        with self.assertRaises(YamlError):
            load_yaml(self.fixture_dir() / "does-not-exist.yaml")


class SchemaValidateTests(unittest.TestCase):
    def test_valid_document_produces_no_errors(self):
        self.assertEqual(validate({"name": "abc", "count": "one"}, MINIMAL_SCHEMA), [])

    def test_missing_required_key_is_reported(self):
        errors = validate({"name": "abc"}, MINIMAL_SCHEMA)
        self.assertTrue(any("count" in e for e in errors))

    def test_pattern_mismatch_is_reported(self):
        errors = validate({"name": "ABC", "count": "one"}, MINIMAL_SCHEMA)
        self.assertTrue(any("name" in e for e in errors))

    def test_enum_mismatch_is_reported(self):
        errors = validate({"name": "abc", "count": "several"}, MINIMAL_SCHEMA)
        self.assertTrue(any("count" in e for e in errors))

    def test_unknown_key_is_reported_when_additional_properties_false(self):
        errors = validate({"name": "abc", "count": "one", "extra": 1}, MINIMAL_SCHEMA)
        self.assertTrue(any("extra" in e for e in errors))

    def test_array_min_items_enforced(self):
        errors = validate({"name": "abc", "count": "one", "tags": []}, MINIMAL_SCHEMA)
        self.assertTrue(any("tags" in e for e in errors))

    def test_ref_resolution_reaches_the_referenced_def(self):
        # "name" is defined purely via $ref -- if resolution were broken this
        # would either crash or silently accept anything.
        errors = validate({"name": "123", "count": "one"}, MINIMAL_SCHEMA)
        self.assertTrue(any("name" in e for e in errors))


class MobileGeneratorTests(unittest.TestCase):
    def tmp_repo_root(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_render_writes_the_brand_display_name_as_a_dart_const(self):
        root = self.tmp_repo_root()
        manifest = {"brand": {"id": "acme", "display_name": "Acme"}}
        written = mobile.render("acme", manifest, root)
        self.assertEqual(written, [root / "app/lib/flavors.brand.dart"])
        content = (root / "app/lib/flavors.brand.dart").read_text()
        self.assertIn("const String kBrandDisplayName = 'Acme';", content)
        self.assertIn("brand/acme/manifest.yaml", content)

    def test_render_escapes_an_apostrophe_in_the_display_name(self):
        root = self.tmp_repo_root()
        manifest = {"brand": {"id": "acme", "display_name": "Acme's App"}}
        mobile.render("acme", manifest, root)
        content = (root / "app/lib/flavors.brand.dart").read_text()
        self.assertIn("const String kBrandDisplayName = 'Acme\\'s App';", content)

    def test_render_escapes_a_dollar_sign_so_dart_does_not_interpolate_it(self):
        # Unescaped, $ starts Dart string interpolation -- 'Ac$me' fails to
        # compile with "Undefined name 'me'." rather than producing a leak.
        root = self.tmp_repo_root()
        manifest = {"brand": {"id": "acme", "display_name": "Ac$me"}}
        mobile.render("acme", manifest, root)
        content = (root / "app/lib/flavors.brand.dart").read_text()
        self.assertIn("const String kBrandDisplayName = 'Ac\\$me';", content)

    def test_render_escapes_an_embedded_newline(self):
        # An unescaped literal newline breaks out of the single-quoted Dart
        # string entirely ("String starting with ' must end with '.").
        root = self.tmp_repo_root()
        manifest = {"brand": {"id": "acme", "display_name": "Acme\nCorp"}}
        mobile.render("acme", manifest, root)
        content = (root / "app/lib/flavors.brand.dart").read_text()
        self.assertIn("const String kBrandDisplayName = 'Acme\\nCorp';", content)

    def test_render_is_idempotent(self):
        root = self.tmp_repo_root()
        manifest = {"brand": {"id": "acme", "display_name": "Acme"}}
        mobile.render("acme", manifest, root)
        first = (root / "app/lib/flavors.brand.dart").read_text()
        mobile.render("acme", manifest, root)
        second = (root / "app/lib/flavors.brand.dart").read_text()
        self.assertEqual(first, second)


class RepoFixture:
    """A throwaway repo with the real brand/ + scripts/brand/ tooling copied in,
    plus a synthetic omi-upstream manifest small enough to assert against
    directly, and one representative file per scanned surface."""

    def __init__(self, root: Path) -> None:
        self.root = root
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=root,
            check=True,
            env={"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(root)},
        )
        (root / "brand/_schema").mkdir(parents=True)
        (root / "brand/omi-upstream").mkdir(parents=True)
        (root / "scripts/brand").mkdir(parents=True)
        (root / "app/lib/l10n").mkdir(parents=True)
        (root / "desktop/macos/Desktop/Sources").mkdir(parents=True)

        schema_src = (BRAND_SCRIPTS.parent.parent / "brand/_schema/manifest.schema.json").read_text()
        (root / "brand/_schema/manifest.schema.json").write_text(schema_src)

        manifest_src = (BRAND_SCRIPTS.parent.parent / "brand/omi-upstream/manifest.yaml").read_text()
        (root / "brand/omi-upstream/manifest.yaml").write_text(manifest_src)

        for name in ("apply.py", "check.py", "schema_validate.py", "yaml_lite.py", "lexicon.yaml"):
            (root / "scripts/brand" / name).write_text((BRAND_SCRIPTS / name).read_text())
        shutil.copytree(BRAND_SCRIPTS / "generators", root / "scripts/brand/generators")

        (root / "brand/_allow.yaml").write_text(
            "schema_version: 1\nexemptions:\n"
            "  \"app/lib/l10n/allowed.arb\":\n"
            "    words: [\"Omi\"]\n"
            "    reason: \"test fixture: deliberately exempted file\"\n"
        )

        (root / "app/lib/l10n/leaky.arb").write_text('{\n  "greeting": "Welcome to Omi"\n}\n')
        (root / "app/lib/l10n/allowed.arb").write_text('{\n  "greeting": "Welcome to Omi"\n}\n')
        (root / "desktop/macos/Desktop/Sources/Hello.swift").write_text(
            'struct Hello: View {\n  var body: some View {\n    Text("Powered by Omi")\n  }\n}\n'
        )

    def run_check(self, brand: str, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.root / "scripts/brand/check.py"), "--brand", brand, "--json", *extra],
            cwd=self.root,
            capture_output=True,
            text=True,
        )

    def run_apply(self, brand: str, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.root / "scripts/brand/apply.py"), "--brand", brand, *extra],
            cwd=self.root,
            capture_output=True,
            text=True,
        )


class DesktopGeneratorTests(unittest.TestCase):
    def render(self, prefix: str, tmp: Path):
        sys.path.insert(0, str(BRAND_SCRIPTS))
        from generators import desktop

        manifest = {"brand": {"id": "test-brand"}, "identifiers": {"macos_named_bundle_prefix": prefix}}
        written = desktop.render("test-brand", manifest, tmp)
        return written, (tmp / desktop.APP_CONFIG_BRAND_PATH).read_text()

    def test_renders_both_prefix_forms_from_one_manifest_field(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        written, content = self.render("com.acme.", tmp)
        self.assertEqual(len(written), 1)
        self.assertIn('OMI_NAMED_BUNDLE_ID_PREFIX="com.acme."', content)
        self.assertIn('OMI_NAMED_BUNDLE_SLUG_PREFIX="acme"', content)

    def test_matches_the_real_omi_upstream_manifest(self):
        # The actual value app-config.sh's fallback must keep matching.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        _, content = self.render("com.omi.", tmp)
        self.assertIn('OMI_NAMED_BUNDLE_ID_PREFIX="com.omi."', content)
        self.assertIn('OMI_NAMED_BUNDLE_SLUG_PREFIX="omi"', content)

    def test_rejects_a_prefix_with_no_segments(self):
        sys.path.insert(0, str(BRAND_SCRIPTS))
        from generators import desktop

        with self.assertRaises(ValueError):
            desktop._named_bundle_slug_prefix(".")


class BrandToolingTests(unittest.TestCase):
    def fixture(self) -> RepoFixture:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return RepoFixture(Path(tmp.name))

    def test_apply_on_the_real_omi_upstream_manifest_is_clean(self):
        # The actual acceptance test B0 is built to satisfy, run against the
        # actual shipped manifest -- not a synthetic one.
        proc = subprocess.run(
            [sys.executable, str(BRAND_SCRIPTS / "apply.py"), "--brand", "omi-upstream", "--check-clean"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_apply_rejects_an_unknown_brand(self):
        fx = self.fixture()
        proc = fx.run_apply("does-not-exist")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no manifest at", proc.stdout + proc.stderr)

    def test_apply_rejects_an_unknown_only_category(self):
        fx = self.fixture()
        proc = fx.run_apply("omi-upstream", "--only", "not-a-real-category")
        self.assertNotEqual(proc.returncode, 0)

    def test_check_finds_the_leak_and_respects_the_exemption(self):
        fx = self.fixture()
        proc = fx.run_check("a-real-fork-brand")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        # leaky.arb's "Omi" must be counted; allowed.arb's identical "Omi"
        # must not be, because it is named in brand/_allow.yaml.
        self.assertEqual(payload["lexicon_matches"], 2)  # leaky.arb + Hello.swift, not allowed.arb

    def test_check_on_omi_upstream_reports_a_self_check_count_not_a_failure(self):
        fx = self.fixture()
        proc = fx.run_check("omi-upstream")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["self_check"])
        self.assertGreater(payload["lexicon_matches"], 0)

    def test_check_without_exemption_would_have_failed(self):
        # Proves the exemption in the previous test is load-bearing: remove
        # it and the same fixture must report one more leak.
        fx = self.fixture()
        (fx.root / "brand/_allow.yaml").write_text("schema_version: 1\nexemptions:\n")
        proc = fx.run_check("a-real-fork-brand")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["lexicon_matches"], 3)

    def test_baseline_ratchet_rejects_an_increase(self):
        fx = self.fixture()
        baseline = fx.root / "baseline.txt"
        first = fx.run_check("a-real-fork-brand", "--baseline", str(baseline))
        self.assertEqual(first.returncode, 0)
        self.assertEqual(baseline.read_text().strip(), "2")

        # A brand out of nowhere growing its leak count must fail, even
        # though check.py's own default (no --baseline) mode already fails
        # on any nonzero count -- --baseline is for CI ratcheting.
        (fx.root / "app/lib/l10n/leaky2.arb").write_text('{"x": "Another Omi mention"}\n')
        second = fx.run_check("a-real-fork-brand", "--baseline", str(baseline))
        self.assertEqual(second.returncode, 1)
        self.assertEqual(baseline.read_text().strip(), "2")  # unchanged on failure


if __name__ == "__main__":
    unittest.main(verbosity=2)
