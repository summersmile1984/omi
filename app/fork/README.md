# Flutter native identity (local Android)

`prepare.py` stages the actual mobile application with one Better Auth owner and
an explicit `self_hosted.local` or `cloudflare.local` profile. It leaves the
upstream source, tests, lockfiles and generated files unchanged. This first
package supports Android debug artifacts. iOS extensions/App Groups/signing,
mobile release qualification, OAuth, remote push and complete capability/UI
branding remain separate deliverables. This package projects selected Flutter
runtime and Android debug rasters; untranslated Omi copy is still not a
white-label acceptance result.

## Owners and credential boundary

- `app/lib/fork/identity/`: the production HTTP protocol, opaque session owner,
  secure storage and public deployment decoder. Only an opaque session with its
  server-confirmed UID can restore a login. A JWT-only legacy cache is not a
  session. The session namespace binds package, target/stage and auth authority.
- `AuthService` remains the typed JWT refresh/401/replay owner. The staged
  constructor selects `IdentityOwner` at its existing `AuthTokenGateway` seam;
  its generation checks, request timeouts, retry budget and terminal event are
  retained. Client issued-at cache admission allows 60 seconds of device clock
  skew; see `contracts/auth/client-cache-admission.md`. Backend/WS services
  verify JWT signatures and live session state.
- The staged `AuthenticationProvider` preserves `clearAllUserState` and
  `AccountCutoverRuntime.bindAuthenticatedOwner`. New identity commits clear
  previous user data before notifying the app. Unavailable restore keeps the
  opaque credential for retry; ordinary sign-out revokes remotely before local
  removal. Destructive settings cleanup uses the same credential commit queue,
  so a newer login cannot publish while old preferences are clearing. A
  401-confirmed terminal session clears the local identity. Name edits commit through
  the same owner; only its auth-provider projection writes given/family name.
  Settings and onboarding await that commit before showing success or advancing.
- `source-owners.json` records the reviewed input hashes; `dart_overlay.dart`
  uses the analyzer already locked by upstream build_runner to select actual
  declaration spans. It does not scan braces or edit tracked source files.
- `overlays/*.dart.txt` are compiled production input, materialized only in the
  stage. `tests/gateway_test.dart.txt` executes the resulting AuthService and
  authenticated request replayer. `.txt` prevents the upstream analyzer from
  type-checking an overlay against the wrong, unmodified source owners.

The local artifact materializes its fork-owned basic local-notification service
with a neutral color and never initializes Firebase/FCM, Intercom or remote
crash/analytics sinks. Upstream retired this service, so the stage cannot depend
on an upstream source file for its no-FCM path.
Remote push and OAuth are explicitly disabled. Existing Android background
readers receive only the derived short-lived JWT mirror; refreshing from a
background-only native engine beyond JWT expiry is **not** qualified here.

## Selected raster assets

The same manifest `icon_master`, `logo_light`, `logo_dark` and `splash` inputs
pass `scripts/brand/raster/png.mjs` before any staged asset is replaced. The
Flutter generator writes the existing generated-Assets paths for the runtime
logo, launcher source and splash, plus every Android density used by legacy,
adaptive, monochrome, notification and Android 12 splash resource consumers.
Main, dev and prod launcher names are all replaced even though this package only
admits local debug builds. Missing, corrupt, escaping, animated, transparent or
oversized inputs fail and remove the partial stage; there is no upstream-image
fallback.

The staged Flutter test loads the actual `Assets.images.herologo`, launcher and
splash paths through `rootBundle` and the engine image decoder. The portable
Node test decodes every Android output at its required density. This does not
compile an APK or prove a device launcher, OEM mask, notification tray, iOS
catalog, App Group, signing or store asset. `fork/asset-coverage.json` records
the exact input/output hashes and these remaining boundaries.

## Build and verify

Use Flutter **3.44.5** with its sibling Dart **3.12.2**, Python 3.12, and the
unchanged `app/pubspec.lock`. No FlutterFire configure or production config is
needed. `make setup` installs the repository hooks.

```bash
cd app
flutter pub get --enforce-lockfile
cd ..
bash app/fork/test.sh

mkdir -p /tmp/mobile-proof
python3 app/fork/fixture.py /tmp/mobile-proof > /tmp/mobile-proof/brand.json
python3 app/fork/prepare.py --manifest /tmp/mobile-proof/brand.json \
  --target self_hosted --output /tmp/mobile-proof-stage --dart "$(command -v dart)"
cd /tmp/mobile-proof-stage/app
flutter pub get --offline --enforce-lockfile
dart run build_runner build
flutter build apk --debug --flavor dev --no-pub --dart-define-from-file=../defines.json
```

Outputs include a public `defines.json`, `build-manifest.json`, and the staged
app. Package IDs are `<manifest android_application_id_dev>.forktest.<target>`.
The stager rejects the upstream brand and existing/repository output paths.
Native class/Pigeon/JNI namespaces remain their protocol names; application
identity changes independently. Published/OAuth deep links are removed. The
Gradle app graph rejects release/profile packaging and no signing inputs are
copied. Native dependency resolution retains upstream Gradle declarations;
their dynamic versions are not a reproducible release lock.

Use only a new, explicitly named simulator and the generated package. If a
backend listens on host loopback, use `adb -s <own-device> reverse tcp:<port>
tcp:<port>` for each fixture endpoint. Launch with `flutter run --debug --flavor
dev --dart-define-from-file=../defines.json -d <own-device>` and follow the
repository `app/e2e/SKILL.md` using agent-flutter. Exercise real account creation,
login, restart/restore, API JWT refresh, revocation and the visible error path.
Never seed a JWT, issuer secret or onboarding completion preference to bypass
the user path.

## Acceptance gates

The existing fork manifest registers `fork-flutter-native-identity` in local and
CI lanes. The existing fork workflow installs pinned Flutter only when the
shared diff selector includes that check. It runs production controller tests,
source/identity boundary checks and both staged Dart application bundles.
These hermetic gates do not claim a native APK compile or emulator interaction.
Live fixture/native evidence belongs in `VERIFICATION.md`; external provider,
signing, push, hardware and distribution qualification requires separate proof.
