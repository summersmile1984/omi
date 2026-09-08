# Native screenshot request deadline — 2026-09-08

Source base: `3107c067d1`, plus the fork staging change described here.
The upstream screenshot feature already uploads candidate pixels. Commit
`3e5af5203a` (upstream PR #12135) contains `MeetingFrameJudge` reading up to eight
local images, Base64-encoding them and calling the authenticated
`POST /v1/screen-frame-egress/adjudications` endpoint. Server privacy review
precedes persisted images, but candidate bytes are uploaded before review.
Cloudflare did not introduce that product behavior.

## Failure and correction

The upstream endpoint uses the shared `OmiHTTPTransport` request timeout of
30 seconds. Cloudflare reviews candidates sequentially; even the two-frame
hosted run exceeded that budget. Current per-candidate external wait limits are
45 seconds for Images transformation/readback, 90 for Qwen and 30 for the
approved-image writer. Those are source limits, not expected model latency.

The fork stage replaces only `APIClient.adjudicateScreenFrames`, under a
compiler-located, SHA256-reviewed source owner. Both deployment targets use
one request inactivity allowance of 60 seconds plus 180 per candidate, clamped
to one through eight candidates: 240–1,500 seconds. This permits the existing
synchronous server workflow to finish without inheriting the ordinary CRUD
deadline. It does not guarantee server completion within that time.

The original attempt, every candidate, image digest and Base64 payload stay
unchanged. There is no added retry, split, client verdict, storage write or
prompt change. Other endpoints retain their existing request budget. The
upstream source file remains byte-for-byte unchanged in the repository.

## Verification

`bash desktop/macos/fork/test.sh` with Node 22 and Python 3.14 on `PATH` passed:

- Two brand asset tests.
- Sixteen Swift Testing cases (the separate XCTest summary has zero cases).
- Nineteen Python staging tests, including the new compiled screenshot probe.

The new test runs in the existing `fork-macos-native-identity` lane. It compiles
the actual staged endpoint, current generic POST/GET functions, screenshot wire
models, transport initializer and JSON encoder/decoder. A controlled response
delivery seam makes the original generic POST time out, while the staged
one- and eight-candidate calls complete. It checks candidate bytes and attempt
identity by comparing decoded JSON; JSON object key order is not a wire
requirement. Server 503 reaches the caller without another request, and the
settings endpoint retains the ordinary timeout. Authentication is controlled
in this probe, so it is not an auth acceptance result.

An additional operator-run loopback check used the same compiled request
functions and the real macOS Foundation URLSession. The HTTP fixture delayed
its response for 41 seconds:

| Native request | Observed result |
| --- | --- |
| Original generic POST | `URLError.timedOut` after 30.527 seconds |
| Staged adjudication POST | Complete decoded response after 41.006 seconds |
| Request contents | Two requests, same attempt and candidate JSON |

The fixture and process exited successfully and closed the loopback listener.
This proves the real transport failure and correction; the fixture does not
execute AI, Cloudflare, account/session logic, screenshot capture or app UI.
The existing hosted screenshot evidence remains separate.

Evidence archive:
`/Users/macstudio/.codex/eddy-production/screen-frame-native-20260908/`.
The operator driver is `eddy-screen-native-live-20260908.py`; its successful
output is `transport-evidence.json`. No user image or credential was used.

## Signed artifact

The ordinary `desktop/macos/fork/release.py` entry freshly staged and built
`/private/tmp/eddy-macos-production-20260908-a/Eddy.app`, version `0.1.0`, build
`2026090801`. Release compilation completed in 480.90 seconds. All 914 staged
Swift source hashes matched the frozen build manifest. The packaged profile
matches that stage byte-for-byte, selects `cloudflare.production`, and forbids
environment URL overrides.

The existing Developer ID Application identity for team `YTY9KEWQC5` signed the
app. `codesign --verify --deep --strict --verbose=2` passed. The exact packaged
executable ran its synthetic Keychain write/read/delete canary successfully and
exited. Its SHA256 is
`c7e1288dff199db1e2c5ff3d99449be27ae3f4bd61503b12c8f9e4d37720ed9b`.
The ZIP is `Eddy-0.1.0-2026090801-macos.zip` in the same output directory.

This arm64 artifact declares macOS 26.0; its existing bundled WebP dependency
was built for that floor. No supported-OS expansion is claimed. The manifest
retains `notarized=false`, `service_verified=false` and `release_ready=false`.
No normal app startup, native screenshot capture or production login was
executed in this run. Signature and canary evidence are in the same archive.

## Still required

This timeout correction does not solve a maximum-size multi-image request.
Full batch-body admission, native capture/upload against hosted services,
queued account erasure and the common two-target screenshot contract remain
unqualified. The screenshot routes retain their existing migration gate;
full Eddy production deployment and native production acceptance are unfinished.

At `2026-09-08T04:03:58Z`, unauthenticated urllib observations of the packaged
Edge health, Auth session and Web origins returned 403; the inspected Edge
response came from Cloudflare and contained `error code: 1010`. This is not an
application-route result. A separate authenticated management-API observation
at `04:06:04.499Z` confirmed all nine expected Eddy production Workers absent.
No Worker or production resource was deployed or modified in this run.
