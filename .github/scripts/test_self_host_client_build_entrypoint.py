#!/usr/bin/env python3
"""Behavioral smoke for the secret-free Better Auth mobile build entrypoint."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class SelfHostClientBuildEntrypointTest(unittest.TestCase):
    def test_desktop_optional_signed_capabilities_do_not_abort_bundle_build(self) -> None:
        script = (ROOT / 'desktop' / 'macos' / 'run.sh').read_text(encoding='utf-8')
        function_body = script.split('update_app_deployment_profile() {', 1)[1].split(
            '\n}\n\nrewrite_bundled_dylib_load_path()', 1
        )[0]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / 'bin'
            fake_bin.mkdir()
            fake_sed = fake_bin / 'sed'
            fake_sed.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
            fake_sed.chmod(0o755)
            env_file = root / '.env'
            env_file.touch()
            harness = f'''#!/usr/bin/env bash
set -e
fail() {{ return 1; }}
update_app_deployment_profile() {{{function_body}
}}
unset OMI_SHARE_BASE_URL OMI_REALTIME_MODEL_PROVIDER
unset OMI_MCP_CHATGPT_OAUTH_CLIENT_ID OMI_MCP_CLAUDE_OAUTH_CLIENT_ID
update_app_deployment_profile "$1"
printf 'completed\\n'
'''
            result = subprocess.run(
                ['bash', '-c', harness, 'test-harness', str(env_file)],
                env={'PATH': f'{fake_bin}:/usr/bin:/bin', 'OMI_DEPLOYMENT_PROFILE': 'omi_cloud'},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('completed', result.stdout)

    def test_desktop_self_host_artifacts_scrub_nested_google_identity_config(self) -> None:
        recursive_scrub = "find \"$APP_BUNDLE/Contents/Resources\" -type f -name 'GoogleService-Info.plist' -delete"
        run_script = (ROOT / 'desktop' / 'macos' / 'run.sh').read_text(encoding='utf-8')
        release_workflow = (ROOT / 'codemagic.yaml').read_text(encoding='utf-8')

        self.assertIn(recursive_scrub, run_script)
        self.assertIn(recursive_scrub, release_workflow)

    def test_flutter_release_selects_better_auth_without_firebase_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory)
            command_log = fake_bin / 'commands.log'
            for command in ('flutter', 'dart'):
                executable = fake_bin / command
                executable.write_text(
                    '#!/usr/bin/env bash\nprintf \'%s\\n\' "$0 $*" >> "$SELF_HOST_COMMAND_LOG"\n',
                    encoding='utf-8',
                )
                executable.chmod(0o755)
            env = {
                'PATH': f'{fake_bin}:/usr/bin:/bin',
                'SELF_HOST_COMMAND_LOG': str(command_log),
                'OMI_AUTH_PROVIDER': 'better_auth',
                'OMI_AUTH_SERVER_URL': 'https://auth.example.com',
                'OMI_API_BASE_URL': 'https://api.example.com',
                'OMI_PRIVACY_URL': 'https://legal.example.com/privacy',
                'OMI_TERMS_URL': 'https://legal.example.com/terms',
                'OMI_SHARE_BASE_URL': 'https://share.example.com',
            }
            result = subprocess.run(
                ['bash', 'release.sh'],
                cwd=ROOT / 'app',
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = command_log.read_text(encoding='utf-8')
            self.assertIn('--dart-define=OMI_AUTH_PROVIDER=better_auth', commands)
            self.assertIn('--dart-define=OMI_APP_PROFILE=self_hosted', commands)
            self.assertIn('--flavor selfhost', commands)
            self.assertIn('--dart-define=OMI_FIREBASE_SERVICES_ENABLED=false', commands)
            self.assertIn('--dart-define=OMI_AUTH_SERVER_URL=https://auth.example.com', commands)
            self.assertIn('--dart-define=OMI_PRIVACY_URL=https://legal.example.com/privacy', commands)
            self.assertIn('--dart-define=OMI_TERMS_URL=https://legal.example.com/terms', commands)
            self.assertIn('--dart-define=OMI_SHARE_BASE_URL=https://share.example.com', commands)
            self.assertNotIn('FIREBASE_SERVICE_ACCOUNT', commands)

    def test_windows_flutter_release_entrypoint_has_the_same_identity_contract(self) -> None:
        script = (ROOT / 'app' / 'setup' / 'scripts' / 'release.ps1').read_text(encoding='utf-8')

        self.assertIn('"-t", "lib/main.dart"', script)
        self.assertNotIn('main_prod.dart', script)
        self.assertIn('OMI_AUTH_SERVER_URL is required when OMI_AUTH_PROVIDER=better_auth', script)
        self.assertIn('"self_hosted"', script)
        self.assertIn('"selfhost"', script)
        self.assertIn('OMI_API_BASE_URL is required when OMI_AUTH_PROVIDER=better_auth', script)
        self.assertIn('foreach ($publicOrigin in @("OMI_PRIVACY_URL", "OMI_TERMS_URL", "OMI_SHARE_BASE_URL"))', script)
        self.assertIn('$publicOrigin is required when OMI_AUTH_PROVIDER=better_auth', script)
        self.assertIn('$env:OMI_FIREBASE_SERVICES_ENABLED = "false"', script)
        self.assertIn('--dart-define=OMI_FIREBASE_SERVICES_ENABLED=false', script)

    def test_android_self_host_flavor_cannot_inherit_managed_native_credentials(self) -> None:
        gradle = (ROOT / 'app' / 'android' / 'app' / 'build.gradle').read_text(encoding='utf-8')
        selfhost = gradle.split('selfhost {', 1)[1].split('\n        }', 1)[0]
        manifest = (ROOT / 'app' / 'android' / 'app' / 'src' / 'selfhost' / 'AndroidManifest.xml').read_text(
            encoding='utf-8'
        )

        self.assertIn('buildConfigField "String", "INTERCOM_APP_ID", \'""\'', selfhost)
        self.assertIn('buildConfigField "String", "INTERCOM_ANDROID_API_KEY", \'""\'', selfhost)
        self.assertIn('android:usesCleartextTraffic="false"', manifest)

    def test_ios_self_host_entrypoint_uses_a_distinct_signed_identity(self) -> None:
        script = (ROOT / 'app' / 'setup.sh').read_text(encoding='utf-8')

        self.assertIn('OMI_SELF_HOST_BUNDLE_ID:-com.friend-app-with-wearable.ios12.selfhost', script)
        self.assertIn('generate_ios_self_host_config omi ', script)
        better_auth_branch = script.split('if [[ "${OMI_AUTH_PROVIDER:-firebase}" == "better_auth" ]]', 1)[1].split(
            'else', 1
        )[0]
        self.assertNotIn('setup_firebase', better_auth_branch)

    def test_ios_self_host_generator_selects_vendor_free_native_authority(self) -> None:
        generator = ROOT / 'app' / 'scripts' / 'generate_ios_self_host_config.sh'
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / 'ios' / 'Flutter'
            result = subprocess.run(
                [
                    'bash',
                    str(generator),
                    str(output_dir),
                    'org.example.memory.selfhost',
                    'memory-auth',
                    'group.org.example.memory.selfhost',
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            config = (output_dir / 'Custom.xcconfig').read_text(encoding='utf-8')
            self.assertIn('APP_BUNDLE_IDENTIFIER=org.example.memory.selfhost', config)
            self.assertIn('APP_GROUP_IDENTIFIER=group.org.example.memory.selfhost', config)
            self.assertIn('AUTH_CALLBACK_SCHEME=memory-auth', config)
            self.assertIn('RUNNER_INFOPLIST_FILE=Runner/Info-SelfHost.plist', config)
            self.assertIn('RUNNER_CODE_SIGN_ENTITLEMENTS=Runner/RunnerSelfHost.entitlements', config)
            self.assertIn('FIREBASE_SERVICES_ENABLED=NO', config)
            self.assertNotIn('GOOGLE', config.upper())

            invalid = subprocess.run(
                ['bash', str(generator), str(output_dir), 'org.example;unsafe', 'memory-auth'],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(invalid.returncode, 2)
            self.assertIn('invalid self-hosted bundle id', invalid.stderr)

    def test_ios_release_entrypoint_passes_complete_profile_and_restores_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / 'app' / 'scripts'
            flutter_dir = root / 'app' / 'ios' / 'Flutter'
            fake_bin = root / 'bin'
            scripts.mkdir(parents=True)
            flutter_dir.mkdir(parents=True)
            fake_bin.mkdir()
            for name in (
                'build_ios_self_host_release.sh',
                'generate_ios_self_host_config.sh',
                'smoke_ios_self_host_artifact.sh',
            ):
                shutil.copy2(ROOT / 'app' / 'scripts' / name, scripts / name)
            original_config = '// developer-local config\nAPP_BUNDLE_IDENTIFIER=local.example\n'
            custom_config = flutter_dir / 'Custom.xcconfig'
            custom_config.write_text(original_config, encoding='utf-8')
            analysis_options = root / 'app' / 'analysis_options.yaml'
            original_analysis = 'include: package:flutter_lints/flutter.yaml\n'
            analysis_options.write_text(original_analysis, encoding='utf-8')
            artifact = root / 'app' / 'build' / 'ios' / 'iphoneos' / 'Runner.app'
            artifact.mkdir(parents=True)
            (artifact / 'Info.plist').write_bytes(
                plistlib.dumps(
                    {
                        'CFBundleIdentifier': 'org.example.memory.selfhost',
                        'CFBundleURLTypes': [
                            {
                                'CFBundleURLSchemes': ['memory-auth'],
                            }
                        ],
                    }
                )
            )
            command_log = root / 'commands.log'
            for command in ('flutter', 'dart'):
                executable = fake_bin / command
                executable.write_text(
                    '#!/usr/bin/env bash\n'
                    'printf \'%s\\n\' "$0 $*" >> "$SELF_HOST_COMMAND_LOG"\n'
                    'if [[ -n "${SELF_HOST_MUTATE_ANALYSIS:-}" ]]; then printf \'mutated\\n\' > "$SELF_HOST_ANALYSIS_PATH"; fi\n'
                    'if [[ "${SELF_HOST_FAIL_BUILD:-}" == "true" && "${1:-}" == "build" ]]; then exit 7; fi\n',
                    encoding='utf-8',
                )
                executable.chmod(0o755)
            env = {
                'PATH': f'{fake_bin}:/usr/bin:/bin',
                'SELF_HOST_COMMAND_LOG': str(command_log),
                'SELF_HOST_ANALYSIS_PATH': str(analysis_options),
                'SELF_HOST_MUTATE_ANALYSIS': 'true',
                'OMI_API_BASE_URL': 'https://api.example.com',
                'OMI_AUTH_SERVER_URL': 'https://auth.example.com',
                'OMI_PRIVACY_URL': 'https://legal.example.com/privacy',
                'OMI_TERMS_URL': 'https://legal.example.com/terms',
                'OMI_SHARE_BASE_URL': 'https://share.example.com',
                'OMI_SELF_HOST_BUNDLE_ID': 'org.example.memory.selfhost',
                'OMI_SELF_HOST_APP_GROUP_ID': 'group.org.example.memory.selfhost',
                'OMI_SELF_HOST_AUTH_CALLBACK_SCHEME': 'memory-auth',
                'OMI_IOS_NO_CODESIGN': 'true',
            }
            result = subprocess.run(
                ['bash', str(scripts / 'build_ios_self_host_release.sh')],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(custom_config.read_text(encoding='utf-8'), original_config)
            self.assertEqual(analysis_options.read_text(encoding='utf-8'), original_analysis)
            commands = command_log.read_text(encoding='utf-8')
            self.assertIn('flutter pub get', commands)
            self.assertIn('dart run build_runner build', commands)
            self.assertIn('flutter build ios --release --flavor prod -t lib/main.dart', commands)
            self.assertIn('--dart-define=OMI_APP_PROFILE=self_hosted', commands)
            self.assertIn('--dart-define=OMI_AUTH_PROVIDER=better_auth', commands)
            self.assertIn('--dart-define=OMI_FIREBASE_SERVICES_ENABLED=false', commands)
            self.assertIn('--no-codesign', commands)

            managed_config = artifact / 'GoogleService-Info.plist'
            managed_config.write_text('managed', encoding='utf-8')
            smoke = subprocess.run(
                [
                    'bash',
                    str(scripts / 'smoke_ios_self_host_artifact.sh'),
                    str(artifact),
                    'org.example.memory.selfhost',
                    'memory-auth',
                    'false',
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(smoke.returncode, 1)
            self.assertIn('packaged GoogleService-Info.plist', smoke.stderr)
            managed_config.unlink()

            env['SELF_HOST_FAIL_BUILD'] = 'true'
            result = subprocess.run(
                ['bash', str(scripts / 'build_ios_self_host_release.sh')],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 7)
            self.assertEqual(custom_config.read_text(encoding='utf-8'), original_config)
            self.assertEqual(analysis_options.read_text(encoding='utf-8'), original_analysis)

    def test_ios_self_host_plist_entitlements_and_resources_have_no_official_identity(self) -> None:
        ios = ROOT / 'app' / 'ios'
        info_path = ios / 'Runner' / 'Info-SelfHost.plist'
        entitlements_path = ios / 'Runner' / 'RunnerSelfHost.entitlements'
        info = plistlib.loads(info_path.read_bytes())
        entitlements = plistlib.loads(entitlements_path.read_bytes())
        serialized = repr(info).lower()

        self.assertNotIn('h.omi.me', serialized)
        self.assertNotIn('googleusercontent', serialized)
        self.assertEqual(
            info['CFBundleURLTypes'][0]['CFBundleURLSchemes'],
            ['$(AUTH_CALLBACK_SCHEME)'],
        )
        self.assertNotIn('aps-environment', entitlements)
        self.assertNotIn('com.apple.developer.associated-domains', entitlements)
        self.assertEqual(entitlements['com.apple.security.application-groups'], ['$(APP_GROUP_IDENTIFIER)'])

        project = (ios / 'Runner.xcodeproj' / 'project.pbxproj').read_text(encoding='utf-8')
        self.assertNotIn('GoogleService-Info.plist in Resources', project)
        self.assertNotIn('Config in Resources', project)
        self.assertNotIn('xcconfig in Resources', project)
        self.assertEqual(project.count('/scripts/copy_google_service_plist.sh'), 2)

    def test_ios_firebase_copy_phase_removes_stale_resource_when_disabled(self) -> None:
        copy_script = ROOT / 'app' / 'ios' / 'scripts' / 'copy_google_service_plist.sh'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'build' / 'Runner.app' / 'GoogleService-Info.plist'
            target.parent.mkdir(parents=True)
            target.write_text('stale-managed-credential', encoding='utf-8')
            stale_nested = target.parent / 'Config' / 'Prod' / 'GoogleService-Info.plist'
            stale_nested.parent.mkdir(parents=True)
            stale_nested.write_text('nested-managed-credential', encoding='utf-8')
            stale_config = target.parent / 'prodRelease.xcconfig'
            stale_config.write_text('GOOGLE_REVERSE_CLIENT_ID=managed', encoding='utf-8')
            env = {
                'PATH': '/usr/bin:/bin',
                'PROJECT_DIR': str(root / 'ios'),
                'TARGET_BUILD_DIR': str(root / 'build'),
                'UNLOCALIZED_RESOURCES_FOLDER_PATH': 'Runner.app',
                'CONFIGURATION': 'Release-prod',
                'FIREBASE_SERVICES_ENABLED': 'NO',
            }
            result = subprocess.run(['/bin/sh', str(copy_script)], env=env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(target.exists())
            self.assertFalse(stale_nested.exists())
            self.assertFalse(stale_config.exists())

            source = root / 'ios' / 'Config' / 'Prod' / 'GoogleService-Info.plist'
            source.parent.mkdir(parents=True)
            source.write_text('official-client-config', encoding='utf-8')
            env['FIREBASE_SERVICES_ENABLED'] = 'YES'
            result = subprocess.run(['/bin/sh', str(copy_script)], env=env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_text(encoding='utf-8'), 'official-client-config')


if __name__ == '__main__':
    unittest.main()
