# Native authentication verification — 2026-09-04

Base: `a2d53f3d00a1c574448cf67a63811d1bb0c51279` on
`codex/implement-whitelabel`. Source ownership pins refer to the audited upstream
app already in this base. All changes are fork-owned. Logs are under
`/tmp/memweft-implementation/whitelabel/macos/` on the implementation host.

## Deterministic execution

- `PATH="$PWD/backend/.venv/bin:$PATH" bash desktop/macos/fork/test.sh`:
  exit 0, **11 Swift Testing tests** (one parametrized across both targets), plus
  **4 Python stage tests**. The latter compiles and runs actual staged AppBuild
  and DesktopStorageIdentity Swift functions. `swift test` also prints an empty
  XCTest runner: the separately reported 11 Swift Testing tests are the actual
  assertion execution. Log: `native-contracts.log`.
- Tests execute opaque authentication/restore/revoke requests; owner-bound JWT
  caching and invalidation; Keychain write failure and five namespaces; malformed
  deployment rejection; ad-hoc code-version identity; expired `get-session` HTTP
  200/null versus malformed responses; wrong-password classification; and
  `iat=Int.min` rejection without integer overflow. The delayed JWT test completes
  an old exchange after invalidation and requires a new network exchange.
- The Swift compiler owner digest mutation test is **static staging coverage**;
  it is not counted as runtime owner, auth or UI verification.
- Pinned Swift formatter 602.0.0 and Black 26.5.1 ran on the new files.
- Explicit final staged-file manifest invocation selected 20 files and passed all 14
  upstream checks. The history-dependent failure-class guard reported a shallow
  history **SKIP**, so its 90-day recurrence scan was not exercised. The fork
  manifest selected native identity + upstream-touch; both passed, with zero
  upstream files touched. Logs: `upstream-staged-preflight-final.log` and
  `fork-staged-preflight.log`. Earlier `scripts/fork/preflight --base HEAD` had
  zero committed upstream diff and is not the change-scope evidence.
- `make preflight` initially stopped because there is no PR metadata for this
  local branch; with `OMI_PR_BODY_FILE` pointing to the local review body it
  passed 15 checks (exit 0). This cumulative 34-file `origin/main..HEAD` scope
  contains the earlier WL1/Web commits, so it is recorded separately from the
  explicit 20-file native scope above. Log:
  `make-preflight-cumulative-with-body.log`.

The first committed 20-file diff subsequently exposed a changelog gate gap:
`desktop-changelog-entry` internally reads committed diff rather than the runner’s
staged file selection, so its earlier “No desktop changes” result was not the
actual release-marker check. The committed invocation passed nine checks
(including one history SKIP), then failed the tenth check for a missing fragment;
four remaining upstream checks and the fork phase were not reached in that run.
Log: `postcommit-preflight.log`. The follow-up adds an internal `kind:none`
fragment for this local fork build package; it does not claim an upstream Omi
release change and does not edit generated `CHANGELOG.json`. The committed-range
wrapper is rerun with that fragment, recorded in `postcommit-preflight-final.log`.

## Actual native artifact and services

`build.py` was run against a temporary **different brand** (`Fixture Notebook`),
not the upstream brand self-check. It compiled the actual staged upstream app
with Swift 6.3.3 and packaged a named, ad-hoc app. Stage 4 build completed 1,897
steps; code signing verification returned exit 0. `app-build-4.log` and
`stage-4/{stage-manifest,artifact-manifest}.json` record it.

The app was launched directly from its temporary path, with an explicitly unique
bridge port 33410; health confirmed exact bundle/PID and the selected API origin
before authenticated actions. No upstream `run.sh` auth/settings seed was used;
no production app or user account was stopped, opened or copied.

Cloudflare profile: actual Auth workerd 33058, Edge 33062, realtime 33063, Python
core 33064, synthetic ASR 33065. Named bundle `com.fixture.omi-native-auth`:

1. Native SwiftUI email/password form signed into a synthetic Better Auth account.
2. A new ad-hoc artifact did not import the prior artifact's Keychain item. The
   **same** artifact was reopened and restored its opaque session through the real
   identity service; the app reported signed in, not restoring.
3. Existing `AuthService.getIdToken` obtained a JWT, reused its memory cache, and
   forced another exchange. Both resulting protected API reads returned 200.
4. Unmodified `TranscriptionService` used its real `/v4/listen` Authorization
   header path, sent one 3,200-byte synthetic PCM frame and decoded one segment.
   No microphone opened. This proves auth/wire/decoder handling with a synthetic
   ASR response, not paid provider quality or a natural voice/PTT flow.
5. Existing full `AuthService.signOut` revoked the remote session, cleared its
   Keychain item and published signed-out state. The previously issued JWT then
   received 401 from the selected protected API.

Evidence: `live-refresh-cf.json`, `live-restored-cf.json`,
`live-restored-health-cf.json`, `live-ws-cf.json`, `live-signout-cf.json`.
The bridge never returned session tokens, JWTs or transcript content.

The first rebuilt ad-hoc experiment froze in `SecItemCopyMatching`: the existing
silent Keychain helper could still wait on the older binary's ACL. The sample is
`native-hang-sample.txt`. The fork now scopes no-Team items by Apple's immutable
code-version identity, fails on unknown signature identity, and never queries
those earlier items. This was repaired without deleting old credentials or
relaxing Keychain access control. A real wrong-password UI attempt also exposed
an expired-session message; authentication now reports rejected input, with a
production transport regression. The parent review found the negative-iat
integer overflow; that precise payload now fails the production decoder test.

Standard Server profile: actual Node/PG Auth 33067 and Python API 33068,
`com.fixture.omi-native-server`, final current source in `stage-server-final`:
native form sign-up succeeded; same-artifact restart restored the opaque session;
first/cache/forced JWT reads returned 200; full logout cleared the Keychain item
and the old JWT received 401. The final wrong-password UI remained signed out and
showed “Unable to sign in. Check your details and try again.” Logs:
`live-{health,refresh,restored,signout}-server.json`,
`native-live-server*.log`, `app-build-server-final.log` (1,897 steps, exit 0).
Server provider/audio quality was not exercised; the native header WS contract
was exercised against the actual CF realtime chain above.

The parent independently executed `ci_build.py --output <new path>` without
`--dependency-cache`: all 1,897 compile steps succeeded in 95.32 seconds.
SwiftPM’s system cache still had hits, so this is not described as entirely
cache-free. Log: `root-ci-cold-stage.log`. It did not package, sign or launch an
app. The final Server artifact includes both the null-session/wrong-password
classification and negative-iat decoder fixes; CF stage 4’s successful-path
artifact preceded those additional error-only guards. Their transport behavior
is covered for both targets by the shared deterministic client tests.

## Boundaries

This is local macOS native **authentication/configuration** acceptance. The
onboarding screen still uses upstream Omi text; icons, links, AI personality,
OAuth callbacks, direct-provider capability policy, complete local agent runtime
and beta/production distribution remain unfinished. Sparkle is disabled rather
than declaring a feed/signing identity ready. Homebrew libwebp on this host is
built for macOS 26 and emits a deployment-floor warning when linked into the
macOS 14-minimum app; this artifact does not qualify macOS 14 or a portable
release. External signing/notarization/updater keys were not used.

The current fork suite plus full app compile are not the full upstream Swift
suite, nor the previously failing upstream PR #10 notification-center test.
Nothing here reclassifies that existing upstream failure as caused by branding
or claims it has been resolved. Root integration owns the macOS workflow lane,
including a fresh no-personal-cache compile of the current final source.
