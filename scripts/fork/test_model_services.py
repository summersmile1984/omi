"""Exercise the production Compose selector, preserving data/migration owners."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/self-host'))
sys.path.insert(0, str(ROOT / 'scripts/brand'))
from manifest import load_manifest  # noqa: E402

from model_services import COMPOSE, profile_for, specialize  # noqa: E402


class ModelServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load(COMPOSE.read_text())
        self.values = {'SELF_HOST_BRAND_MANIFEST': 'brand/eddy/manifest.yaml', 'SELF_HOST_STAGE': 'beta'}

    def test_mimo_keeps_state_and_embedding_and_removes_unused_model_services(self):
        original = deepcopy(self.config)
        selected = specialize(self.config, profile_for(self.values))
        self.assertEqual(self.config, original)
        self.assertNotIn('llm', selected['services'])
        self.assertNotIn('llm-artifact-check', selected['services'])
        for name in (
            'postgres',
            'redis',
            'minio',
            'qdrant',
            'typesense',
            'embedding',
            'auth-migrate',
            'firestore-pg-migrate',
            'qdrant-migrate',
        ):
            self.assertEqual(selected['services'][name], original['services'][name])
        backend = selected['services']['backend']
        self.assertIn('MIMO_API_KEY=${MIMO_API_KEY:?MIMO_API_KEY is required}', backend['environment'])
        self.assertFalse(any(':/models/speech:' in value for value in backend['volumes']))
        self.assertIn('firestore-pg-migrate', backend['depends_on'])
        self.assertIn('qdrant-migrate', backend['depends_on'])
        self.assertFalse(
            any(value.startswith('MIMO_') for value in selected['services']['queue-worker']['environment'])
        )

    def test_existing_native_profile_keeps_its_model_services(self):
        self.values['SELF_HOST_STAGE'] = 'local'
        self.assertEqual(specialize(self.config, profile_for(self.values)), self.config)

    def test_malformed_operator_profile_fails_before_compose_mutation(self):
        profile = profile_for(self.values)
        profile['operator_ai']['base_url'] = 'https://unreviewed.example.com/v1'
        with self.assertRaises(ValueError):
            specialize(self.config, profile)

    def _hosted_values(self, name):
        import json
        import uuid

        harness = ROOT / 'backend/.harness/test-model-services'
        harness.mkdir(parents=True, exist_ok=True)
        manifest = load_manifest('eddy', ROOT)
        manifest['self_hosted_inference'] = {'local': 'native', 'beta': name}
        path = harness / f'hosted-{name}-{uuid.uuid4().hex}.json'
        path.write_text(json.dumps(manifest))
        self.addCleanup(path.unlink, missing_ok=True)
        values = dict(self.values)
        values['SELF_HOST_BRAND_MANIFEST'] = str(path)
        return values

    def test_hosted_operator_removes_every_model_service_and_binds_its_credentials(self):
        from fork.operator_ai import CLOUDFLARE_TOKEN_ENV, CREDENTIAL_ENV

        for name in ('openrouter', 'cloudflare-gateway'):
            values = self._hosted_values(name)
            if name == 'cloudflare-gateway':
                manifest = json.loads(Path(values['SELF_HOST_BRAND_MANIFEST']).read_text())
                manifest['cloudflare_ai_gateway'] = {'account_id': 'a' * 32, 'gateway_id': 'prod'}
                Path(values['SELF_HOST_BRAND_MANIFEST']).write_text(json.dumps(manifest))
            self.config = yaml.safe_load(COMPOSE.read_text())
            selected = specialize(self.config, profile_for(values))
            for service in ('llm', 'llm-artifact-check', 'embedding', 'embedding-artifact-check'):
                self.assertNotIn(service, selected['services'])
            for stateful in ('postgres', 'redis', 'minio', 'qdrant', 'typesense', 'firestore-pg-migrate'):
                self.assertIn(stateful, selected['services'])
            for service in selected['services'].values():
                self.assertNotIn('embedding', service.get('depends_on', {}))
                self.assertFalse(
                    any(value.startswith('EMBEDDING_ENDPOINT=') for value in service.get('environment') or [])
                )
            credential_envs = [CREDENTIAL_ENV[name]]
            if name == 'cloudflare-gateway':
                credential_envs.append(CLOUDFLARE_TOKEN_ENV)
            for holder in ('backend', 'memory-maintenance-worker'):
                service = selected['services'].get(holder)
                if service is not None:
                    for env in credential_envs:
                        self.assertIn(f'{env}=${{{env}:?{env} is required}}', service['environment'])
            backend = selected['services']['backend']
            self.assertFalse(any(':/models/speech:' in value for value in backend['volumes']))
            self.assertFalse(
                any(value.startswith('MIMO_') for value in selected['services']['queue-worker']['environment'])
            )

    def test_hosted_operator_profile_has_no_local_model_rows(self):
        values = self._hosted_values('openrouter')
        profile = profile_for(values)
        for key in ('llm', 'speech', 'embedding'):
            self.assertNotIn(key, profile)
        self.assertEqual(profile['capabilities']['stt_providers'], ['openrouter'])


if __name__ == '__main__':
    unittest.main()
