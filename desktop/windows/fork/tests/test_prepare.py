import hashlib
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prepare import ROOT, COMPONENT, prepare, verify_source_owners
from ci_build import synthetic_manifest


class SourceStageContract(unittest.TestCase):
    def test_both_real_targets_have_distinct_identity_and_closed_build_inputs(self):
        with tempfile.TemporaryDirectory(prefix='electron-stage-test-') as temporary:
            root = Path(temporary)
            manifest = root / 'brand.json'
            manifest.write_text(json.dumps(synthetic_manifest(root)))
            original = (COMPONENT / 'src/main/index.ts').read_bytes()
            identities = []
            for target in ('self_hosted', 'cloudflare'):
                stage = prepare(manifest, target, root / target)
                profile = json.loads((stage / 'fork/profile.json').read_text())
                record = json.loads((root / target / 'build-manifest.json').read_text())
                self.assertEqual(
                    record['fork_source_hashes']['native/owner.ts'],
                    hashlib.sha256((COMPONENT / 'fork/native/owner.ts').read_bytes()).hexdigest(),
                )
                identities.append(profile['applicationId'])
                self.assertEqual(profile['target'], target)
                self.assertEqual(profile['updates'], 'disabled')
                self.assertEqual(profile['personaName'], 'Synthetic Guide')
                self.assertEqual(set(record['assets']['inputs']), {'icon_master', 'logo_light', 'logo_dark'})
                for path, value in record['assets']['outputs'].items():
                    self.assertEqual(hashlib.sha256((stage / path).read_bytes()).hexdigest(), value['sha256'])
                self.assertIn('splash', record['assets']['unconsumed'])
                self.assertFalse((stage / 'resources/icon.png').exists())
                self.assertFalse((stage / 'src/renderer/src/assets/omi-logo.png').exists())
                coverage = json.loads((stage / 'fork/brand-coverage.json').read_text())
                self.assertGreater(len(coverage['rendered']), 40)
                self.assertTrue(any('omi-capture:cmd' == row['source'] for row in coverage['preserved']))
                self.assertEqual((stage / 'pnpm-lock.yaml').read_bytes(), (COMPONENT / 'pnpm-lock.yaml').read_bytes())
                self.assertFalse((stage / 'src/renderer/src/lib/firebase.ts').exists())
                package = json.loads((stage / 'package.json').read_text())
                for script in package['scripts'].values():
                    self.assertNotIn('--config electron-builder.config.mjs', script)
                    if 'electron-builder.fork.config.mjs' in script:
                        self.assertIn('--publish never', script)
                # Static closure tripwire, additional to executed main/renderer
                # tests and complete compiler/bundler acceptance in ci_build.
                generated = (stage / 'electron.vite.config.ts').read_text()
                self.assertIn(profile['apiBase'], generated)
                self.assertNotIn('authStore:', (stage / 'src/preload/index.ts').read_text())
                self.assertNotIn("https://api.omi.me/*", (stage / 'src/main/application.ts').read_text())
                self.assertEqual(
                    (stage / 'src/main/ipc/pimono.test.ts').read_bytes(),
                    (COMPONENT / 'src/main/ipc/pimono.test.ts').read_bytes(),
                )
            self.assertEqual(len(set(identities)), 2)
            self.assertEqual((COMPONENT / 'src/main/index.ts').read_bytes(), original)

    def test_new_presentation_text_cannot_silently_escape_the_catalogue(self):
        # Execute the production stage renderer with a new upstream string in a
        # covered owner. Hash drift is separately covered above; this catches a
        # hash refresh which forgot to classify the new presentation itself.
        script = '''
import { applyBrandPresentation } from './fork/brand-stage.mjs';
const read = path => path.endsWith('.json') ? JSON.stringify({'owner.tsx': []}) : process.argv[1];
try { applyBrandPresentation('.', {links: {}}, {read}); process.exit(2) }
catch (error) { if (!error.message.includes('Unclassified brand text')) throw error }
'''
        for source in (
            '<p>Welcome to Omi</p>',
            '<p title={`Ask Omi ${name}`} />',
            '<p title={`${name} asks Omi ${other}`} />',
            '<p title={`${name} asks Omi`} />',
        ):
            with self.subTest(source=source):
                subprocess.run(['node', '--input-type=module', '-e', script, source], cwd=COMPONENT, check=True)

    def test_source_owner_drift_and_unsafe_output_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix='electron-owner-test-') as temporary:
            root = Path(temporary)
            file = root / 'owner.ts'
            file.write_text('reviewed owner')
            owners = {'owner.ts': hashlib.sha256(file.read_bytes()).hexdigest()}
            verify_source_owners(root, owners)
            file.write_text('unreviewed change')
            with self.assertRaisesRegex(ValueError, 'owner drift'):
                verify_source_owners(root, owners)
            link = root / 'symlink'
            link.symlink_to(root)
            for output in (link, ROOT / 'forbidden-stage', root):
                with self.assertRaises(ValueError):
                    prepare(root / 'missing-brand.json', 'self_hosted', output)

    def test_legacy_upstream_brand_is_not_a_local_identity_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'upstream identity'):
                prepare(ROOT / 'brand/omi-upstream/manifest.yaml', 'self_hosted', Path(temporary) / 'stage')


if __name__ == '__main__':
    unittest.main()
