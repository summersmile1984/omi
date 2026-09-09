"""Exercise the production Compose selector, preserving data/migration owners."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/self-host'))
from model_services import COMPOSE, profile_for, specialize


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


if __name__ == '__main__':
    unittest.main()
