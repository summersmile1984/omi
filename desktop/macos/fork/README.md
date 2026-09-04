# macOS native deployment consumer

This package stages the current upstream Swift app with a selected fork profile.
It is the first **local native authentication** acceptance package, not a signed
white-label distribution. No command installs, launches, seeds, or stops an app.
Use only a synthetic `omi-*` named bundle when running it.

## Boundary and ownership

`prepare.py` calls the same brand manifest loader and profile resolver as the
server/web builds. Only `self_hosted.local` and `cloudflare.local` are admitted by
this first packager; missing or invalid configuration fails closed. Public
origins are embedded in `ForkDeployment.json`, never credentials. The staged
`DesktopBackendEnvironment` is authoritative for all existing API and native
WebSocket callers. The bundle does not load `.env` files, Firebase emulator
variables, or debug bearer injection. OAuth providers are not advertised until
the native provider/callback contract is implemented.

`NativeAuthClient` performs Better Auth email sign-in/sign-up, restores the
opaque session through `/api/auth/get-session`, exchanges it at `/api/auth/token`
for a session-bound one-hour JWT, and remotely signs out. It uses an ephemeral,
cookie-free, redirect-refusing URLSession and explicit Bearer session headers.
Only the opaque session is persisted. JWTs stay in an owner-bound memory cache;
cache invalidation also revokes publication by outstanding exchanges. A 401 is
an expired/revoked session; transport/5xx/storage failure preserves recoverable
credentials. API/WS services remain responsible for signature and live-session
verification.

The existing `AuthService` attempt fence, `AuthSessionCoordinator`, and
`RuntimeOwnerIdentity` still own local identity transitions. The adapter commits
Keychain + defaults within that fence, after owner storage is prepared. Logout
revokes the remote session before the existing local cleanup transaction. A
cleanup/storage failure is reported instead of claiming successful logout.
There is no Firebase custom-token bridge and no legacy credential migration.
An existing upstream principal signs in explicitly into the selected namespace.

Keychain service identity includes the selected keychain prefix, signing team,
exact bundle ID, deployment target/stage, and auth authority digest. Ad-hoc code
also uses the code-version identity returned by Apple `kSecCodeInfoUnique`;
unknown signing identity fails closed. A different ad-hoc build requires a fresh
sign-in, while reopening the same artifact restores its own session. This avoids
the foreign-ACL wait reproduced by rebuilding the same local bundle on 2026-09-04.
The identity behavior follows [Apple’s code identity contract](https://developer.apple.com/documentation/security/kseccodeinfounique). The adapter
reuses `DesktopKeychainStore` for silent OS access; it never queries upstream
Firebase accounts. Named storage is rooted at `<brand-id> Dev Bundles/<bundle>`.
Upstream custom-bundle production classification and shared-storage fallback
were reproduced during audit B1; this package executes the staged classification
and storage code as regression coverage. Sparkle admission is disabled, feed/key
entries and URL callback schemes are removed from this local artifact.

## Source staging

Only this fork directory changes. `swift_overlay.py` asks the Swift compiler for
function declaration ranges and verifies reviewed SHA256 owners before replacing
bootstrap/auth entry points. Replaced whole modules have exact owners too.
Changed owners require review of the current upstream behavior and an explicit
`source-owners.json` update. There is no automatic hash acceptance mode. Other
upstream auth cleanup and API/WS callers stay in the staged app. `stage-manifest.json`
records source owner hashes and every staged Swift source hash.

The overlay is a build input, not a generated source commit. Temporary manifests,
stages, SwiftPM outputs, signed apps and live credentials stay outside Git.
`build.py` copies only checkout/artifact dependency caches, not compiled module
caches with absolute paths. SwiftPM uses the upstream lock with automatic
resolution disabled. The ad-hoc artifact manifest explicitly says
`qualification=local-native-auth-only` and `release_ready=false`.

## Commands

Prerequisites: macOS/Xcode (verified here with Swift 6.3.3), Python >=3.10 and the
brand tooling dependencies. The upstream Desktop package dependencies must be
available at their locked revisions for a full build.

```sh
# Hermetic consumer behavior + actual staged identity execution + owner tripwire.
bash desktop/macos/fork/test.sh

# A private synthetic manifest must have explicit local deployment origins.
python3 desktop/macos/fork/build.py \
  --manifest /tmp/native-brand.json --target cloudflare \
  --app-name omi-native-auth --output /tmp/new-native-stage \
  --dependency-cache /path/to/Desktop/.build
```

The `fork-macos-native-identity` entry in the existing fork check manifest runs
both local and CI lanes on macOS. Full compile CI entry: `python3 desktop/macos/fork/ci_build.py --output /tmp/new-native-ci`
(optionally `--dependency-cache`). It creates its own synthetic manifest, uses
locked dependency resolution, and neither packages/signs nor launches the app.

The production transport/cache/storage tests
are behavioral; compiler owner drift is explicitly a static tripwire. Full app
compilation and live services are separate evidence, never counted as hermetic
unit test coverage. Root integration owns the existing workflow macOS runner
and the frozen dependency preparation for its full app compilation gate.

For a live check, launch only the resulting named app executable with a unique
`OMI_AUTOMATION_PORT`. Verify the unauthenticated `omi-ctl health` exact bundle,
PID and selected origins before authenticated bridge requests. Sign in through
the real native form with a synthetic account. Reopen the same named bundle to
verify OS Keychain restoration. The existing authenticated local bridge has a
fork action (DEBUG + named local only):

```sh
OMI_AUTOMATION_PORT=33410 desktop/macos/scripts/omi-ctl action fork_native_identity operation=refresh
OMI_AUTOMATION_PORT=33410 desktop/macos/scripts/omi-ctl action fork_native_identity operation=transcription
OMI_AUTOMATION_PORT=33410 desktop/macos/scripts/omi-ctl action fork_native_identity operation=signout
```

`refresh` executes production `AuthService.getIdToken` and protected API calls.
`transcription` executes the unmodified production `TranscriptionService` with
one explicit synthetic PCM frame and reports segment counts, never opens a mic.
It proves credential/header/wire handling only when a local ASR fixture is used.
`signout` invokes full production logout, checks Keychain removal and an old JWT
against the selected protected API. None returns tokens or transcript contents.
These live checks need configured local Auth/API/WS services and stay outside CI.

## Remaining distribution acceptance

This first package does not claim beta/production signing, notarization, updater
feed isolation, universal architecture, portable dependency closure or macOS 14
runtime acceptance. The local host linked Homebrew libwebp built for macOS 26;
that must be vendored/qualified before distribution. Upstream onboarding text,
assets, browser/account links and AI personality remain visible after login and
require the subsequent full identity/brand package. OAuth, direct-provider
capability consumption and a packaged local agent runtime remain separate native
acceptance work. Real signing teams/keys and final brand inputs are external
inputs; generic generation and failure tests continue without them.
