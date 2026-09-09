"""Execute the shared classifier with additive fork settings."""

import importlib.util
import sys
import unittest
from check_deployment_settings import ROOT, combine

spec = importlib.util.spec_from_file_location(
    'deployment_setting_checker', ROOT / '.github/scripts/check_deployment_secret_boundary.py'
)
checker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = checker
spec.loader.exec_module(checker)


class DeploymentSettingsTests(unittest.TestCase):
    def setUp(self):
        self.upstream = {'kinds': {'secret': ['EXISTING_SECRET'], 'config': [], 'public_build': ['PUBLIC_KEY']}}
        self.fork = {'kinds': {'secret': ['CF_TOKEN'], 'config': ['DEPLOY_ROOT'], 'public_build': []}}

    def test_shared_checker_accepts_secret_and_config_sources(self):
        policy = combine(self.upstream, self.fork)
        self.assertEqual(checker.validate_policy(policy), [])
        bindings = [
            checker.Binding('fork.yml', 'github_secrets', 'CF_TOKEN'),
            checker.Binding('fork.yml', 'github_vars', 'DEPLOY_ROOT'),
        ]
        self.assertEqual(checker.validate_bindings(policy, bindings, []), [])

    def test_shared_checker_still_rejects_wrong_secret_source(self):
        binding = checker.Binding('fork.yml', 'github_vars', 'CF_TOKEN')
        self.assertTrue(checker.validate_bindings(combine(self.upstream, self.fork), [binding], []))

    def test_fork_cannot_override_upstream_classification_or_exceptions(self):
        self.fork['kinds']['config'].append('EXISTING_SECRET')
        with self.assertRaises(ValueError):
            combine(self.upstream, self.fork)
        self.fork['exceptions'] = {}
        with self.assertRaises(ValueError):
            combine(self.upstream, self.fork)


if __name__ == '__main__':
    unittest.main()
