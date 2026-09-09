# Terminal raster verification — 2026-09-05

Base: `f426815d2d985e1770eb6b7ac48789be46d178c1`. Candidate branch:
`codex/implement-native-assets`. All brand inputs are disposable H/N fixtures
under temporary directories; no formal brand, signing key, credential or
production application was used. Raw logs are in
`/tmp/memweft-implementation/native-assets/`.

## Shared boundary and platform consumers

`npm ci --prefix scripts/brand/raster` installs only locked `pngjs@7.0.0`.
`npm test --prefix scripts/brand/raster` executes two production-boundary tests:
valid static PNG decoding/alpha-correct resize and rejection of corrupt,
animated, transparent, escaping and traversal inputs. Electron imports this
module in place of its byte-identical private copy. A current generation of all
nine Electron output assets has exactly the same SHA256 values as the artifact
used by the previously accepted Login/About/LegacyHome/WebGL UI run; see
`electron-accepted-equivalence.json`. The full Electron fork gate also passes:
14 core tests, four prepare tests, 16 staged tests and a complete build for each
target. This preserves that earlier macOS-host Electron evidence; it does not
rerun or expand it into Windows/Linux system UI or installer qualification.

The macOS lightweight gate passes two generator tests, 12 Swift identity tests
and eight Python stage tests. The latter execute the real stager, compile the
actual staged sign-in view with its brand consumer, decode both selected images
through AppKit, and exercise missing/corrupt images. They also verify the local
packager rejects stale SwiftPM resource bytes, installs the selected ICNS and
projects manifest display name/icon metadata. No full Desktop Swift build,
codesign, application launch or release action ran for this candidate. The
existing `fork-macos-native-compile` check remains selected for macOS CI and was
deliberately not run locally under the task's resource limit.

The Flutter gate passes 14 core identity tests, two portable Android asset
tests, four stager tests, and 19 staged tests plus a real Dart application bundle
for each target. Each staged test loads `Assets.images.herologo`, launcher and
splash through Flutter's `rootBundle` and engine decoder. The portable test
decodes all 71 generated Flutter/Android PNG outputs at their density-specific
dimensions. This is not an APK, emulator, device launcher, notification tray or
iOS catalog qualification. Prior accepted APKs and screenshots remain separate
identity evidence and were not modified or relaunched.

## Build and CI boundary

`fork-brand-raster` is registered in both fork-manifest lanes and cites the
actual Electron-only decoder ownership fixed by this package. Changes in the
shared raster module select Electron, Flutter and macOS consumers. The Linux job
installs the tiny npm package only when a selected raster/Flutter/Electron check
needs it. The selected macOS job installs Node 22 and the same frozen package
before its existing identity and compile checks. No upstream workflow,
component source, tests or lockfile changed.

The pending tree contains 35 changed paths: 33 product, test or documentation
paths and two CI paths (`.github/checks-manifest.fork.yaml` and
`.github/workflows/fork-checks.yml`). Those CI changes are intentionally listed
separately for integration because the upstream-sync workflow has another
owner. The exact Linux fork-manifest run selected and passed nine checks:
Electron, Flutter, CI diff-base, upstream-touch, profile tables, brand tooling,
shared raster, upstream-brand cleanliness and profile-boundary tests. The exact
macOS exclusive selection contains `fork-macos-native-identity` and
`fork-macos-native-compile`; the former passed through the direct component
gate above, while the latter was selected but not run.

The upstream manifest selected and passed 20 checks. Its failure-class history
ratchet reported a shallow-history `SKIP`, so that row is not full-history
evidence. The desktop changelog-entry check also cannot observe an uncommitted
tree because it resolves `base...HEAD`; it printed that no desktop files
changed. This tree does contain
`desktop/macos/changelog/unreleased/20260905-fork-native-assets-local.json`, but
the changelog-entry check must be repeated after integration creates a commit.
The exact logs are `fork-linux-exact.log`, `fork-macos-selection.json` and
`upstream-exact.log` in the evidence directory named above.

## Remaining work

macOS text-logo, cinematic/notch/chat inline marks, device media, videos and
splash remain unconsumed. Flutter text-logo, device media and every iOS target
remain unconsumed. Formal brand artwork, safe-zone review, Windows/Linux native
system surfaces, iOS extensions/App Groups, release signing/notarization,
installers, updater feeds and app-store assets require separate inputs and real
platform evidence. This candidate is not a whole-bundle visual scan or a
zero-brand-leak assertion.
