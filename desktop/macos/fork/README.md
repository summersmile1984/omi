# macOS native deployment consumer

This package stages the current upstream Swift app with a selected fork profile.
It owns native authentication, selected brand resources and isolated application
packaging. No command installs, launches, seeds, or stops an app. Use an `omi-*`
named identity for development and automated live verification.

## Boundary and ownership

`prepare.py` calls the same brand manifest loader and profile resolver as the
server/web builds. Both targets admit `local`, `beta` and `production`; missing
or invalid configuration fails closed. The deployment stage is independent of
the development/distribution identity, allowing a named app to verify a
production endpoint. Beta/production identities require their matching stage
and manifest application name; they cannot claim an upstream identity. Public
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
entries and URL callback schemes are removed from each artifact. Stable data is
rooted at `<brand-id>`, beta at `<brand-id> Beta`; every fork identity disables
legacy upstream storage migration and the stable-app deletion/termination path.
PostHog initialization is disabled at its SDK owner, including production
classification; the fork does not enroll in the upstream analytics project.

## Source staging

Only this fork directory changes. `swift_overlay.py` asks the Swift compiler for
function declaration ranges and verifies reviewed SHA256 owners before replacing
bootstrap/auth entry points. Replaced whole modules have exact owners too.
Changed owners require review of the current upstream behavior and an explicit
`source-owners.json` update. There is no automatic hash acceptance mode. Other
upstream auth cleanup and API/WS callers stay in the staged app. `stage-manifest.json`
records source owner hashes and every staged Swift source hash.

The native memory importer stages one reviewed `OnboardingMemoryBatchImportService.save`
declaration. For Cloudflare, `NativeMemoryBatching` plans requests of at most
100 items and 1,000,000 bytes using the actual memory wire model and the HTTP
transport's encoder, including metadata, JSON envelope and escaped Unicode.
The Server OS target retains its 100-item grouping. Planning finishes before
any write. An individually oversized item increments the failed count without
discarding its neighbors. Each emitted group remains one ordinary API call;
the existing per-batch saved/failed counters, retry policy and account/session
fences remain owned by the importer. No successful batch is replayed merely
because a later batch failed. The low-level API does not secretly split calls.

The existing native identity lane runs the planner's Swift tests and compiles
and executes the actual staged importer, wire models and encoder with a
controlled API seam. It checks complete ordered delivery, encoded byte limits,
partial failures, oversized-item isolation, account switching and both targets.
It is separate from full-app compilation, real HTTP integration and UI evidence.

The same manifest `icon_master`, `logo_light` and `logo_dark` inputs pass the
shared bounded static-PNG validator before staging replaces any resource. The
platform generator derives the existing Dock, menu-bar and `herologo` resource
names, explicit light/dark sign-in images, and a complete local ICNS container.
The existing AppKit consumers keep their state and layout ownership. The sign-in
view shows an explicit unavailable message if a packaged image is damaged; it
never falls back to an upstream logo. Packaging verifies every selected PNG
byte-for-byte inside SwiftPM's resource bundle, installs the generated ICNS, and
uses the manifest display name in `CFBundleDisplayName` while retaining the
named test executable identity.

The current asset scope does not cover the text logo, cinematic/notch/chat
inline marks, device media, videos or splash. They remain explicit entries in
`brand-assets.json`, so this package is not a whole-app no-leak assertion.

The overlay is a build input, not a generated source commit. Temporary manifests,
stages, SwiftPM outputs, signed apps and live credentials stay outside Git.
`build.py` copies only checkout/artifact dependency caches, not compiled module
caches with absolute paths. SwiftPM uses the upstream lock with automatic
resolution disabled. The ad-hoc artifact manifest explicitly says
`qualification=local-native-auth-and-brand-assets-only` and `release_ready=false`.

## Commands

Prerequisites: macOS/Xcode (verified here with Swift 6.3.3), Node 22 with
`npm ci --prefix scripts/brand/raster`, Python >=3.10 and the brand tooling
dependencies. The upstream Desktop package dependencies must be
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
(optionally `--dependency-cache`). It creates one synthetic private manifest and
compiles isolated `self_hosted.local` and `cloudflare.local` named bundles with
locked dependency resolution. It neither packages/signs nor launches either app.

The production transport/cache/storage tests and AppKit image-decoding tests
are behavioral; compiler owner drift is explicitly a static tripwire. Full app
compilation and live services are separate evidence, never counted as hermetic
unit test coverage. The existing `fork-checks.yml` selects macOS-only checks
with the shared manifest runner, then runs both this suite and
`fork-macos-native-compile` on `macos-26` with Xcode 26.6 and Python 3.12.
`bash desktop/macos/fork/compile.sh` is the same local compile entry. It creates
and removes an isolated stage; no signing, service or personal dependency cache
is required. CI caches SwiftPM downloads only and installs `pkg-config`/`webp`.
The compile is a debug source check, not a supported-OS or distribution proof;
it does not enter the pre-push gate. Remote CI has not yet been executed for
this local candidate.

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

## Developer ID packaging

`release.py` builds an isolated release configuration, prepares the locked
universal Node and agent payload through the upstream scripts copied into that
stage, and assembles a new app bundle. It vendors the actual non-system dylib
dependencies, removes host build rpaths, derives the minimum macOS version from
the Mach-O load commands, and signs nested code before the outer app. Node alone
receives the existing JIT entitlements; the app has no debug or Apple Sign-In
entitlement. The exact Developer ID certificate fingerprint is explicit.

```sh
python3 desktop/macos/fork/release.py \
  --manifest brand/eddy/manifest.yaml --target cloudflare --stage production \
  --app-name Eddy --output /tmp/new-eddy-release \
  --signing-identity <Developer-ID-certificate-SHA1> \
  --version 0.1.0 --build-number 2026090501
```

Optional `--dependency-cache` reuses locked SwiftPM downloads through independent
APFS clones. After deep strict signature verification, the publisher checks the
actual signing team, bundle identity, hardened runtime and secure timestamp,
then runs the existing full bundle dependency audit. The artifact receipt keeps
`release_ready`, `service_verified` and `notarized` false: signing never supplies
remote product or Apple notarization evidence. The local ad-hoc packager still
rejects production profiles and identities.

## Remaining distribution acceptance

Notarization, universal app architecture and macOS 14 runtime acceptance remain
separate checks. The local Homebrew libwebp requires macOS 26; the signed package
must declare that actual floor until a qualified older-OS library is supplied.
Uncovered upstream
onboarding text/assets, browser/account links and AI personality remain visible
after login and require subsequent reviewed packages. OAuth, direct-provider
capability consumption remain separate native acceptance work. Generic identity,
dependency and signing-verifier tests run without a personal certificate; a
real signed artifact and its live client flow are separate required evidence.
