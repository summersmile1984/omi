# WL-7 local artifact matrix verification — 2026-09-05

Candidate: `codex/unified-delivery`, after `fef8e60656`, with the local matrix
changes recorded in this worktree. Every run used a generated synthetic private
brand and loopback/`.example.invalid` deployment input. No private brand asset,
signing credential, production application, device, remote deployment, or
release publisher was used.

## Matrix

| Client surface | `self_hosted` | `cloudflare` | Result |
| --- | --- | --- | --- |
| Android / Flutter | stage, 20 staged tests, debug Dart bundle | stage, 20 staged tests, debug Dart bundle | passed |
| macOS | stage, full named Swift debug binary | stage, full named Swift debug binary | passed |
| Electron | stage, 16 staged tests, typecheck and production bundle | stage, 16 staged tests, typecheck and production bundle | passed |

## Commands and evidence

- `PATH=/private/tmp/memweft-flutter-3.44.5/bin:$PATH bash app/fork/test.sh`
  exited 0 with the pinned Flutter 3.44.5 / Dart 3.12.2 SDK. It passed 14
  native identity tests, two asset tests, five stage tests, then passed 20
  staged tests and compiled a debug Flutter bundle for each target. This is a
  Dart bundle check; it does not claim an APK, iOS build, install, or device
  exercise. The stage owns complete `auth.dart` and `auth_provider.dart`
  overlays, so their upstream bytes are now restored and neither is admitted as
  a source owner; the stage test proves the overlays still replace either input.
- `bash desktop/macos/fork/test.sh` passed two Node resource tests, 12 Swift
  tests, and 11 Python stage/matrix tests. The matrix guard accepts only a
  fresh path outside the repository, assigns each target a separate
  `omi-native-ci-<target>` name, and removes the complete output on any target
  failure. `python3 desktop/macos/fork/ci_build.py --output
  /tmp/memweft-macos-matrix-fef8e60656` emitted two full debug binaries:
  `self_hosted` (254,937,408 bytes) and `cloudflare` (254,933,920 bytes).
  Neither binary was packaged, signed, installed, or launched.
- `npx --yes --package node@22 -- bash fork/test.sh` from `desktop/windows`
  exited 0 with the required isolated Node 22 runtime. It passed 14 base
  tests, four stage tests, then passed 16 staged tests and completed a
  typecheck plus Electron production bundle for each target. The ordinary
  system Node remains untouched.
- Upstream owner drift stopped the Electron stage before it built. The five
  changed reviewed files and the Settings `AdvancedTab` owner were refreshed
  in `desktop/windows/fork/source-owners.json` only after inspecting the
  upstream changes. The staged Settings import is now explicitly tested to
  resolve to `fork/renderer/identity` and not Firebase. `python3 -m unittest
  discover -s fork/tests -p 'test_prepare.py'` passed all four tests after the
  assertion was added.

## Scope and remaining release gates

This gives the fork one repeatable local brand × target × client build matrix.
It does not qualify a release: iOS, native Android APK/IPA compilation, Windows
DPAPI, Linux keyring, installers, code signing, notarization, updater feeds,
store distribution, real brand visual review, OEM device identity, firmware
signing/OTA, remote Server OS deployment, Cloudflare resource apply, secrets,
and hosted end-to-end product acceptance remain separate gates. Release
readiness is false.
