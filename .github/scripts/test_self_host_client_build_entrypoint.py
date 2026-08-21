#!/usr/bin/env python3
"""Behavioral smoke for the secret-free Better Auth mobile build entrypoint."""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
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
            clean_payload = fake_bin / 'clean-payload'
            clean_payload.mkdir()
            (clean_payload / 'payload.bin').write_text('self-host fixture', encoding='utf-8')
            clean_archive = shutil.make_archive(str(fake_bin / 'clean-artifact'), 'zip', clean_payload)
            for command in ('flutter', 'dart'):
                executable = fake_bin / command
                executable.write_text(
                    '#!/usr/bin/env bash\n'
                    'printf \'%s\\n\' "$0 $*" >> "$SELF_HOST_COMMAND_LOG"\n'
                    'if [[ "${1:-}" == "build" && "${2:-}" == "appbundle" ]]; then\n'
                    '  mkdir -p build/app/outputs/bundle/selfhostRelease\n'
                    '  cp "$SELF_HOST_CLEAN_ARCHIVE" build/app/outputs/bundle/selfhostRelease/app-selfhost-release.aab\n'
                    'fi\n'
                    'if [[ "${1:-}" == "build" && "${2:-}" == "apk" ]]; then\n'
                    '  mkdir -p build/app/outputs/flutter-apk\n'
                    '  cp "$SELF_HOST_CLEAN_ARCHIVE" build/app/outputs/flutter-apk/app-selfhost-release.apk\n'
                    'fi\n',
                    encoding='utf-8',
                )
                executable.chmod(0o755)
            env = {
                'PATH': f'{fake_bin}:/usr/bin:/bin',
                'SELF_HOST_COMMAND_LOG': str(command_log),
                'SELF_HOST_CLEAN_ARCHIVE': clean_archive,
                'OMI_AUTH_PROVIDER': 'better_auth',
                'OMI_AUTH_SERVER_URL': 'https://auth.example.com',
                'OMI_API_BASE_URL': 'https://api.example.com',
                'OMI_PRIVACY_URL': 'https://legal.example.com/privacy',
                'OMI_TERMS_URL': 'https://legal.example.com/terms',
                'OMI_SHARE_BASE_URL': 'https://share.example.com',
                'OMI_MCP_BASE_URL': 'https://mcp.example.com',
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
            self.assertIn('--dart-define=OMI_MCP_BASE_URL=https://mcp.example.com', commands)
            self.assertNotIn('FIREBASE_SERVICE_ACCOUNT', commands)
            self.assertIn('flutter pub get --enforce-lockfile', commands)

            command_log.write_text('', encoding='utf-8')
            mismatched = subprocess.run(
                ['bash', 'release.sh'],
                cwd=ROOT / 'app',
                env={**env, 'OMI_APP_PROFILE': 'mobile_beta'},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(mismatched.returncode, 1)
            self.assertIn('requires OMI_APP_PROFILE=self_hosted', mismatched.stderr)
            self.assertEqual(command_log.read_text(encoding='utf-8'), '')

            missing_provider = subprocess.run(
                ['bash', 'release.sh'],
                cwd=ROOT / 'app',
                env={
                    **env,
                    'OMI_APP_PROFILE': 'self_hosted',
                    'OMI_AUTH_PROVIDER': '',
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_provider.returncode, 1)
            self.assertIn('requires OMI_AUTH_PROVIDER=better_auth', missing_provider.stderr)
            self.assertEqual(command_log.read_text(encoding='utf-8'), '')

    def test_windows_flutter_release_entrypoint_has_the_same_identity_contract(self) -> None:
        script = (ROOT / 'app' / 'setup' / 'scripts' / 'release.ps1').read_text(encoding='utf-8')

        self.assertIn('"-t", "lib/main.dart"', script)
        self.assertNotIn('main_prod.dart', script)
        self.assertIn('OMI_AUTH_SERVER_URL is required when OMI_AUTH_PROVIDER=better_auth', script)
        self.assertIn('"self_hosted"', script)
        self.assertIn('"selfhost"', script)
        self.assertIn('OMI_API_BASE_URL is required when OMI_AUTH_PROVIDER=better_auth', script)
        self.assertIn(
            'foreach ($publicOrigin in @("OMI_PRIVACY_URL", "OMI_TERMS_URL", "OMI_SHARE_BASE_URL", "OMI_MCP_BASE_URL"))',
            script,
        )
        self.assertIn('$publicOrigin is required when OMI_AUTH_PROVIDER=better_auth', script)
        self.assertIn('$env:OMI_FIREBASE_SERVICES_ENABLED = "false"', script)
        self.assertIn('--dart-define=OMI_FIREBASE_SERVICES_ENABLED=false', script)
        self.assertIn('requires OMI_APP_PROFILE=self_hosted for this release lane', script)
        self.assertIn('requires OMI_AUTH_PROVIDER=better_auth for this release lane', script)
        self.assertIn('$appProfile -eq "self_hosted"', script)
        self.assertIn('Invoke-CheckedNative "flutter" @("pub", "get", "--enforce-lockfile")', script)
        self.assertIn('self-host build changed pubspec.lock', script)
        self.assertIn('[Convert]::ToBase64String([IO.File]::ReadAllBytes($lockFile))', script)
        self.assertIn('function Invoke-CheckedNative', script)
        self.assertIn('if ($LASTEXITCODE -ne 0)', script)
        self.assertIn('Invoke-CheckedNative "flutter" (@("build", "appbundle") + $flutterArgs)', script)
        self.assertIn('Invoke-CheckedNative "flutter" (@("build", "apk") + $flutterArgs)', script)
        self.assertIn('"USE_AUTH_CUSTOM_TOKEN=false"', script)
        self.assertIn('self-host codegen embedded managed client value', script)
        self.assertIn('scripts/smoke_android_self_host_artifact.ps1', script)
        checker = (ROOT / 'app' / 'scripts' / 'smoke_android_self_host_artifact.ps1').read_text(encoding='utf-8')
        self.assertIn('[IO.Compression.ZipFile]::OpenRead', checker)
        self.assertIn('$entry.Open()', checker)
        self.assertIn('[IO.File]::ReadAllBytes', checker)
        self.assertIn('Remove-Item -LiteralPath $scanRoot -Recurse -Force', checker)
        self.assertIn('https?://([^/@\\s]+\\.)?omi\\.me([/:?#]|$)', checker)

    def test_android_self_host_flavor_cannot_inherit_managed_native_credentials(self) -> None:
        gradle = (ROOT / 'app' / 'android' / 'app' / 'build.gradle').read_text(encoding='utf-8')
        selfhost = gradle.split('selfhost {', 1)[1].split('\n        }', 1)[0]
        manifest = (ROOT / 'app' / 'android' / 'app' / 'src' / 'selfhost' / 'AndroidManifest.xml').read_text(
            encoding='utf-8'
        )

        self.assertIn('buildConfigField "String", "INTERCOM_APP_ID", \'""\'', selfhost)
        self.assertIn('buildConfigField "String", "INTERCOM_ANDROID_API_KEY", \'""\'', selfhost)
        self.assertIn('android:usesCleartextTraffic="false"', manifest)
        setup = (ROOT / 'app' / 'setup.sh').read_text(encoding='utf-8')
        selfhost_branch = setup.split('run_build_android selfhost', 1)[0].rsplit('if [[', 1)[-1]
        self.assertNotIn('setup_firebase', selfhost_branch)
        run_build_android = setup.split('function run_build_android()', 1)[1].split(
            '# #####################################', 1
        )[0]
        self.assertIn('firebase_services_enabled=false', run_build_android)
        self.assertIn(
            'OMI_FIREBASE_SERVICES_ENABLED="$firebase_services_enabled" flutter run', run_build_android
        )

    def test_android_self_host_artifact_gate_rejects_packaged_firebase_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_root = root / 'bad'
            (bad_root / 'res' / 'values').mkdir(parents=True)
            (bad_root / 'res' / 'values' / 'google_app_id.xml').write_text('managed', encoding='utf-8')
            artifact = shutil.make_archive(str(root / 'bad-selfhost'), 'zip', bad_root)
            result = subprocess.run(
                ['bash', str(ROOT / 'app' / 'scripts' / 'smoke_android_self_host_artifact.sh'), artifact],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn('managed Firebase configuration', result.stderr)

    def test_android_self_host_artifact_gate_scans_compressed_payload_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / 'bad-credential.zip'
            with zipfile.ZipFile(artifact, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('assets/runtime.bin', 'benign')
                archive.writestr('ASSETS/RUNTIME.BIN', 'compressed-only credential AIza' + 'A' * 35)
            result = subprocess.run(
                ['bash', str(ROOT / 'app' / 'scripts' / 'smoke_android_self_host_artifact.sh'), artifact],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn('populated managed-client credentials', result.stderr)

    def test_android_self_host_artifact_gate_rejects_official_omi_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'bad-official-origin.zip'
            with zipfile.ZipFile(artifact, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('assets/runtime.bin', 'https://api.omi.me/v1')
            result = subprocess.run(
                ['bash', str(ROOT / 'app' / 'scripts' / 'smoke_android_self_host_artifact.sh'), artifact],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn('official Omi-managed origin', result.stderr)

    def test_self_host_codegen_uses_clean_env_and_restores_local_files_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app_root = Path(directory) / 'app'
            scripts = app_root / 'scripts'
            generated_dir = app_root / 'lib' / 'env'
            scripts.mkdir(parents=True)
            generated_dir.mkdir(parents=True)
            shutil.copy2(ROOT / 'app' / 'scripts' / 'self_host_env_guard.sh', scripts)
            env_file = app_root / '.env'
            generated = generated_dir / 'prod_env.g.dart'
            analysis = app_root / 'analysis_options.yaml'
            lock_file = app_root / 'pubspec.lock'
            env_file.write_text('POSTHOG_API_KEY=phc_local_secret\nCUSTOM=keep\n', encoding='utf-8')
            env_file.chmod(0o644)
            generated.write_text('// developer generated state\n', encoding='utf-8')
            analysis.write_text('// developer analysis state\n', encoding='utf-8')
            lock_file.write_text('# developer lock state\n', encoding='utf-8')
            command = scripts / 'mutate-and-fail.sh'
            command.write_text(
                '#!/usr/bin/env bash\n'
                'set -e\n'
                '! grep -q POSTHOG_API_KEY .env\n'
                'grep -Fx "API_BASE_URL=https://api.example.com" .env\n'
                'printf "sanitized generated\n" > lib/env/prod_env.g.dart\n'
                'printf "flutter mutation\n" > analysis_options.yaml\n'
                'exit 7\n',
                encoding='utf-8',
            )
            command.chmod(0o755)
            result = subprocess.run(
                [
                    'bash',
                    '-c',
                    'source scripts/self_host_env_guard.sh; with_self_host_env_guard "$PWD" scripts/mutate-and-fail.sh',
                ],
                cwd=app_root,
                env={**os.environ, 'OMI_API_BASE_URL': 'https://api.example.com'},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(env_file.read_text(encoding='utf-8'), 'POSTHOG_API_KEY=phc_local_secret\nCUSTOM=keep\n')
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o644)
            self.assertEqual(generated.read_text(encoding='utf-8'), '// developer generated state\n')
            self.assertEqual(analysis.read_text(encoding='utf-8'), '// developer analysis state\n')
            self.assertEqual(lock_file.read_text(encoding='utf-8'), '# developer lock state\n')

            succeeded = subprocess.run(
                [
                    'bash',
                    '-c',
                    'source scripts/self_host_env_guard.sh; with_self_host_env_guard "$PWD" true',
                ],
                cwd=app_root,
                env={**os.environ, 'OMI_API_BASE_URL': 'https://api.example.com'},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o644)

    def test_self_host_codegen_rejects_and_restores_dependency_lock_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app_root = Path(directory) / 'app'
            scripts = app_root / 'scripts'
            generated_dir = app_root / 'lib' / 'env'
            scripts.mkdir(parents=True)
            generated_dir.mkdir(parents=True)
            shutil.copy2(ROOT / 'app' / 'scripts' / 'self_host_env_guard.sh', scripts)
            (app_root / '.env').write_text('CUSTOM=keep\n', encoding='utf-8')
            (generated_dir / 'prod_env.g.dart').write_text('// generated\n', encoding='utf-8')
            (app_root / 'analysis_options.yaml').write_text('// analysis\n', encoding='utf-8')
            lock_file = app_root / 'pubspec.lock'
            lock_file.write_text('# pinned lock\n', encoding='utf-8')
            result = subprocess.run(
                [
                    'bash',
                    '-c',
                    'source scripts/self_host_env_guard.sh; '
                    'with_self_host_env_guard "$PWD" bash -c \'printf "changed lock\\n" > pubspec.lock\'',
                ],
                cwd=app_root,
                env={**os.environ, 'OMI_API_BASE_URL': 'https://api.example.com'},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn('self-host build changed pubspec.lock', result.stderr)
            self.assertEqual(lock_file.read_text(encoding='utf-8'), '# pinned lock\n')

    def test_ios_artifact_gate_scans_binary_payload_for_managed_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'Runner.app'
            artifact.mkdir()
            (artifact / 'Info.plist').write_bytes(
                plistlib.dumps(
                    {
                        'CFBundleIdentifier': 'org.example.memory.selfhost',
                        'CFBundleURLTypes': [{'CFBundleURLSchemes': ['memory-auth']}],
                    }
                )
            )
            (artifact / 'Runner').write_bytes(b'compiled-data phc_' + b'A' * 32)
            result = subprocess.run(
                [
                    'bash',
                    str(ROOT / 'app' / 'scripts' / 'smoke_ios_self_host_artifact.sh'),
                    str(artifact),
                    'org.example.memory.selfhost',
                    'memory-auth',
                    'false',
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn('managed-client credentials', result.stderr)

    def test_ios_artifact_gate_rejects_official_omi_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'Runner.app'
            artifact.mkdir()
            (artifact / 'Info.plist').write_bytes(
                plistlib.dumps(
                    {
                        'CFBundleIdentifier': 'org.example.memory.selfhost',
                        'CFBundleURLTypes': [{'CFBundleURLSchemes': ['memory-auth']}],
                    }
                )
            )
            (artifact / 'Runner').write_bytes(b'compiled-data https://api.omi.me/v1')
            result = subprocess.run(
                [
                    'bash',
                    str(ROOT / 'app' / 'scripts' / 'smoke_ios_self_host_artifact.sh'),
                    str(artifact),
                    'org.example.memory.selfhost',
                    'memory-auth',
                    'false',
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn('official Omi-managed origin', result.stderr)

    def test_ios_self_host_entrypoint_uses_a_distinct_signed_identity(self) -> None:
        script = (ROOT / 'app' / 'setup.sh').read_text(encoding='utf-8')

        self.assertIn('OMI_SELF_HOST_BUNDLE_ID:-com.friend-app-with-wearable.ios12.selfhost', script)
        self.assertIn('generate_ios_self_host_config omi ', script)
        better_auth_branch = script.split('if [[ "${OMI_AUTH_PROVIDER:-firebase}" == "better_auth" ]]', 1)[1].split(
            'else', 1
        )[0]
        self.assertNotIn('setup_firebase', better_auth_branch)
        self.assertIn('flutter pub get --enforce-lockfile', script)
        self.assertIn('self-hosted iOS run arguments cannot override', script)

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
                'self_host_env_guard.sh',
                'check_self_host_generated_env.sh',
            ):
                shutil.copy2(ROOT / 'app' / 'scripts' / name, scripts / name)
            original_config = '// developer-local config\nAPP_BUNDLE_IDENTIFIER=local.example\n'
            custom_config = flutter_dir / 'Custom.xcconfig'
            custom_config.write_text(original_config, encoding='utf-8')
            analysis_options = root / 'app' / 'analysis_options.yaml'
            original_analysis = 'include: package:flutter_lints/flutter.yaml\n'
            analysis_options.write_text(original_analysis, encoding='utf-8')
            generated_env = root / 'app' / 'lib' / 'env' / 'prod_env.g.dart'
            generated_env.parent.mkdir(parents=True)
            generated_env.write_text(
                '\n'.join(
                    f'  static final String? {field} = null;'
                    for field in (
                        'posthogApiKey',
                        'googleMapsApiKey',
                        'intercomAppId',
                        'intercomIOSApiKey',
                        'intercomAndroidApiKey',
                        'googleClientId',
                        'googleClientSecret',
                    )
                ),
                encoding='utf-8',
            )
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
                'OMI_MCP_BASE_URL': 'https://mcp.example.com',
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
            self.assertIn('flutter pub get --enforce-lockfile', commands)
            self.assertIn('dart run build_runner build', commands)
            self.assertIn('flutter build ios --release --flavor prod -t lib/main.dart', commands)
            self.assertIn('--dart-define=OMI_APP_PROFILE=self_hosted', commands)
            self.assertIn('--dart-define=OMI_AUTH_PROVIDER=better_auth', commands)
            self.assertIn('--dart-define=OMI_FIREBASE_SERVICES_ENABLED=false', commands)
            self.assertIn('--dart-define=OMI_MCP_BASE_URL=https://mcp.example.com', commands)
            self.assertIn('--no-codesign', commands)

            override_cases = (
                (['--dart-define=OMI_APP_PROFILE=mobile_beta'], 'cannot override OMI_APP_PROFILE'),
                (['--dart-define-from-file=untrusted.json'], 'cannot override build authority'),
                (['--flavor', 'dev'], 'cannot override build authority'),
                (['--target=lib/main_prod.dart'], 'cannot override build authority'),
                (['--debug'], 'cannot override build authority'),
            )
            for override_args, expected_error in override_cases:
                with self.subTest(override_args=override_args):
                    command_log.write_text('', encoding='utf-8')
                    override = subprocess.run(
                        ['bash', str(scripts / 'build_ios_self_host_release.sh'), *override_args],
                        env=env,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(override.returncode, 2)
                    self.assertIn(expected_error, override.stderr)
                    self.assertEqual(command_log.read_text(encoding='utf-8'), '')

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
