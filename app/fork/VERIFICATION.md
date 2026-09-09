# Android native identity verification — 2026-09-04

Baseline: `556dfc2f8c7bf6c30323af19797ff105a70c4b02`; branch
`codex/implement-flutter-auth`. All raw logs and synthetic native artifacts are
under `/tmp/memweft-implementation/flutter/` on the audit host. No upstream
source, lock, generated output, production app or release signing input changed.

## Automated production behavior

- `make setup`: exit 0; linked-worktree hooks installed (`setup.log`).
- Pinned isolated Flutter 3.44.5 / Dart 3.12.2, upstream frozen pubspec lock;
  `bash app/fork/test.sh` runs core owner/protocol tests, three actual stager
  boundary cases, both target staged AuthService and login-widget tests, and
  both actual main Dart bundles. Initial accepted `fork-gate-accepted.log`: exit 0, 14 core tests + three stager cases,
  16 tests per staged target (core + actual gateway + widget), two Dart
  application bundles.
- Final runner `android-acceptance-final-gate.log`: exit 0, 14 core tests,
  three stage cases, 18 tests and a compiled actual main Dart bundle per target.
  This includes the production modal save-error regression: the dialog stays
  visible and contains its own error, rather than a Snackbar behind the modal.
- The delayed settings-clear regression uses the production credential owner:
  an accepted newer login cannot publish until the previous asynchronous clear
  finishes, and its durable credential/UID/preferences survive.
- The name mutation regression executes the staged AuthService through the
  production IdentityOwner: server-confirmed identity changes never let a
  later request continuation overwrite display preferences, and an old delayed
  name response cannot modify a newer login. Both actual name form callers
  await the owner and retain the page on error.
- The actual black-button-theme widget case caught invisible login controls on
  Android; it exercises the production AuthComponent and mode-switch callback.
- `TZ=UTC bash app/test.sh`: exit 0, 1700 tests passed
  (`upstream-app-suite-utc.log`). Initial Asia/Shanghai run had 1694 passes,
  five skips and one existing search day-grouping fixture failure: its UTC
  noon/18:00 timestamps cross local midnight. Isolated UTC case passes; no
  upstream test or expected value changed.
- Analyzer ratchet: passed (`analyzer-ratchet.log`); fork source analyzer:
  no issues (`fork-analyze-final.log`). Existing 48-locale untranslated-message
  warnings remain outside this identity package.

## Native dependency/build boundary

This host uses Android Studio JBR, Android API 36.1 arm64 emulator image and the
unchanged upstream Gradle/native dependency declarations. The SDK lacks
cmdline-tools; existing platform/build-tools/NDKs were used. Three artifacts
could not resolve because the Java Maven TLS connection failed. A disposable
file Maven repository admitted only exact selected versions of
`androidx.lifecycle:lifecycle-livedata:2.10.0`, `com.posthog:posthog:6.34.1`, and
`androidx.browser:browser:1.8.0`, downloaded over verified TLS from official Maven
origins and checked against their published SHA1. Existing cached metadata was
also hash checked. No TLS/certificate validation was disabled.

The pinned Flutter integration_test plugin requests dynamic `runner:1.2+`;
this fixture constrained it to Google's published stable 1.2.0. The temporary
Gradle init/mirror scripts are recorded alongside logs. They alter dependency
resolution only for this disposable stage. This is local native compile proof,
not a reproducible release lock or a claim that a clean runner has every native
dependency. CI's hermetic contract runner compiles both Dart bundles and does
not claim an APK/emulator run. Upstream Opus optional libopusenc-path warnings,
Whisper NDK warnings, and Gradle deprecation warnings are retained in build logs.

Accepted authentication-flow APK builds (`server-accepted-build.log`,
`cf-accepted-build.log`) each passed 994 Gradle tasks: 495 executed, 494 from
cache, five up-to-date. These compile Dart, Kotlin and native JNI libraries.

## Real Android user-path evidence

Only own AVD `omi-fork-flutter`, emulator `emulator-5566`, was created and used.
The synthetic brand is `Native Proof`; application IDs are
`invalid.example.nativeproof.dev.forktest.selfhosted` and
`invalid.example.nativeproof.dev.forktest.cloudflare`. Public manifests contain
loopback/`.example.invalid` origins and no auth signing secrets. The secure
credential namespace binds package, target/stage and auth origin.

Server fixture: real Node/Postgres Auth 33067 + standard API 33068.
Cloudflare fixture: real workerd Auth 33058 + Edge 33062/Realtime 33063 + Python
core 33064; ASR 33065 is a synthetic local transcription provider. ADB reverse
routes only those own endpoints. The initial real registration hit 26 seconds
of device/auth clock skew; the final cache contract accepts at most 60 seconds
without extending expiration or weakening server verification.


Server accepted APK restored an existing opaque credential, refreshed its JWT,
and the actual AuthService HTTP pipeline returned Tasks 200. The real UI then
walked the existing onboarding pages, used the offered voice-profile skip,
reached Home, and used Settings → Sign Out → confirmation. The old JWT was
held only inside the running application probe; after this UI logout the
backend rejected it with 401. Wrong-password error text was visible, and a
subsequent correct sign-in succeeded. Evidence: `server-accepted-run.log`
(`FORK_HTTP_PIPELINE:200`, `FORK_REFRESH:AuthTokenSuccess`,
`FORK_REVOKED_API:401`), `server-restored-state.json`,
`server-logged-out-state.json`, `server-sign-in-state.json`, and the corresponding
`server-{restored-onboarding,home,logged-out,final-wrong-password}.png` captures.

Server product limits are separate from that identity evidence: this older
33068 fixture's `/v1/users/onboarding` returned 500 for an unregistered
Postgres `onboarding_admission` collection; its speech-profile storage endpoint
has an obsolete MinIO endpoint configuration. Initial messages also lack the
self-hosted LLM credential. The UI path reached Home, but persistent complete
first-run onboarding is not qualified. Those server owners are being repaired
independently; no terminal workaround skips or masks their failures.

The separately installed Cloudflare package started signed out while the
Server package retained its own credential (`cf-target-isolated-state.json`).
Actual email registration, JWT refresh and the AuthService Tasks request
passed. During normal onboarding, the actual TranscriptSegmentSocketService
and IOWebSocketChannel used the existing native Authorization header to
`/v4/listen?codec=pcm16`. The UI displayed `Local synthetic transcript` and
reached Home. Evidence: `cf-accepted-run.log`, `cf-native-transcription.png`,
`cf-native-transcription.text.json`, and `cf-home.png`. This proves native
headers, bytes and transcript delivery through Edge/Realtime; the local ASR
fixture is synthetic and proves no physical hardware or model quality.

The initial Cloudflare cold restart found a confirmed backend blocker. The opaque
session restores and authenticated business reads return 200, but
`/v1/announcements/pending` returns 401 twice. The existing terminal-401 owner
then intentionally clears credentials and routes to login; a subsequent valid
sign-in reproduces it. `cf-restored-run.log` records
`FORK_TERMINAL_EVENT:backendRejectedRefreshedToken:null`, corroborated by
`/tmp/memweft-implementation/cloudflare/cf2-local/edge.log`.
`cf-restored.png` and `cf-restored-state.json` are failure evidence, not a
successful restore qualification. The independent CF announcement route repair found a dynamic announcement-ID
route consuming `/pending` before the authenticated static route. With that
real Edge repair loaded, the same unchanged accepted APK signed in, survived
force-stop/cold restart directly to Home, refreshed its JWT and returned Tasks
200 through the actual AuthService HTTP pipeline. Settings → Sign Out → Ok
cleared the owner; an old JWT held only inside the app returned Tasks 401.
`cf-final-restore-run.log` records `FORK_REFRESH:AuthTokenSuccess`,
`FORK_HTTP_PIPELINE:200` and `FORK_REVOKED_API:401`;
`cf-final-restore-state.json` is true and `cf-final-logged-out-state.json` false.
Final captures are `cf-final-restored-home.png` and `cf-final-logged-out.png`.
Both the earlier failure and final passing evidence remain, with no client
401 behavior changed to bypass the backend error. Other CF integrations return
503 where their services are absent; that is outside this identity qualification.

Accepted APKs are preserved in
`/tmp/memweft-implementation/flutter/accepted-apks/android-{server,cf}-accepted.apk`;
`accepted-apks.json` records their hashes. Only reproducible stage build caches
were removed after artifact preservation. `assembleDevRelease --dry-run`
against the actual staged Gradle graph failed as expected before release work
with `Local fork staging supports debug only` (`native-release-rejected.log`).

The final name/projection patch was built and installed as a third CF APK.
`cf-name-final-build.log` passed 994 tasks (496 executed, 493 cached, five
up-to-date). The final inline error component came from another fresh source
stage, copied over the identical native tree for an incremental build;
`cf-name-display-build.log` passed 994 tasks (31 executed, 963 up-to-date).
`accepted-apks/android-cf-final.apk` and `accepted-apks.json` retain that final
artifact/hash. The original Server and CF identity-flow APKs remain separately
identified; the final name UI itself was exercised on CF, while the final
Server Dart source compiles and executes the same hermetic name tests.

In that final installed APK, Settings → Profile → Name was exercised with only
this emulator's Auth reverse port removed. Save displayed `Connection Error`
inside the dialog, retained the old name and signed-in owner, and allowed retry.
Restoring that reverse port and pressing Save confirmed `Native Proof Confirmed`
with server identity and local given/family-name projection agreeing.
`cf-name-visible-offline-error.png`, `cf-name-save-confirmed.png`,
`cf-name-final-error-keeps-owner.json` and `cf-name-confirmed-projection.json`
record these outcomes. The final APK then used actual Settings Sign Out;
`cf-name-display-run.log` records `FORK_REVOKED_API:401`, and
`cf-name-final-logged-out-state.json` is false. The own AVD/apps were stopped
only after this evidence was captured; all external fixtures were left alone.

## Deterministic gate scope

The pre-commit explicit proposed-path run of the upstream check runner selected
18 checks; all passed (`upstream-proposed-scope.log`). The earlier wrapper run
also passed (`preflight.log`), but its upstream committed range was empty and
reported `files=0`; it must not be described as full modified-source coverage.
Fork gates consumed the staged paths and passed. Product-invariant suggestions
matched no invariant. The final proposed Android path run also passed all
18 checks (`android-proposed-gates-final.log`). Final committed-range gate results are recorded with the
commit; no claim is made about the entire historical integration delta.

## Limits

This package qualifies local Android debug identity/profile consumption only.
iOS, release signing, App Groups/extensions, remote push, OAuth, physical
hardware/BLE, background-only JWT renewal, full AI capability UI and complete
white-label text/icon/assets remain separate acceptance scopes. Screens still
contain upstream Omi assets: these screenshots are identity evidence, never a
no-brand-leak qualification. No production mobile/macOS app was operated.
