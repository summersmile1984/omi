# Core screenshot views and revocation

The Cloudflare Core Worker now registers the seven screenshot settings, read,
sharing and deletion routes plus a content proxy. The eight upstream screenshot
route inventory slots remain blocked: candidate adjudication, approval minting,
new-survivor publication and Edge routing are still required. No Worker was
deployed remotely in this change.

## Behavior

- The ordinary build and CPython test runner project the same upstream wire
  models and selection function. Default prompts and adjudication policy are
  unchanged; these routes do not invoke an AI provider.
- Existing accounts default to enabled. Disabling hides persisted frames and
  cancels pending writes through the existing D1 epoch trigger. Re-enabling
  restores retained approved frames. Public reads use the existing unique
  conversation share index and return 200 with an empty set when unavailable.
- Sharing withdrawal does not change the frame-set revision. Shared read
  capabilities become invalid while owner capabilities remain usable.
- Deleting one frame compares revision and epoch before atomically publishing
  survivors, incrementing both counters and marking removed receipts for cleanup.
  It retries a concurrent change instead of restoring a removed frame. The staged
  upstream selector alone determines banner promotion. Delete-all increments both
  counters even for an empty set; adjudication timestamp is preserved.
- URLs use the admitted API origin and a separate HMAC content purpose. The
  proxy forwards only the token to the isolated writer and streams its response.
  The writer rechecks D1 privacy state after fetching R2. A disconnected proxy
  reader cancels the upstream stream and releases its lock. Physical R2 cleanup
  remains the writer's durable scheduled responsibility.
- Unrepresentable legacy frames are omitted with sanitized shared fallback
  telemetry; missing palette uses the unchanged upstream neutral colors. Invalid
  signing configuration produces 503 rather than a misleading successful empty set.

## Verification

`uvx uv==0.12.3 run pytest -q tests/test_screen_frame_views.py` in
`deploy/cloudflare/python/api-core`: **15 passed** initially. An added cancellation-failure case brings the final
Screenshot suite to **16 tests**. The tests call registered
FastAPI routes and execute every production App D1 migration in SQLite. They
cover legacy defaults, owner isolation, request-bound authentication, wire and
read-capability signatures, share/setting revocation, concurrent deletion,
account deletion fences, malformed metadata and stream disconnect. Only service
transport is controlled in this suite; writer behavior has its separate native
D1/R2 coverage.

`env PATH=<pinned Node 22 bin>:$PATH bash deploy/cloudflare/ci/routes.sh`:
**6 route-inventory tests, 619 route slots, manifest/typecheck success,
114 Worker test files / 922 tests, 565 Core tests and 150 AI tests passed**.
CPython Core reported the existing Starlette/AnyIO deprecation warning. The full
command exited zero. This is the Cloudflare component suite, not a Server OS or
desktop release qualification.

After ensuring the reader lock is released even when stream cancellation fails,
`uvx uv==0.12.3 run --project deploy/cloudflare/python/api-core pytest -q
deploy/cloudflare/python/api-core/tests` passed **566 tests**, including all 16
screenshot cases. This final run exited zero with the same existing warning;
evidence is `screen-frame-views-core-final-20260906.log` in the private directory.

An additional private local probe compiled the normal Core entry with the pinned
Python builder and the production storage writer, then started both in actual
workerd. All production App D1 migrations were applied. It seeded two approved
fixture receipts and matching R2 objects through local Wrangler commands; it
did not add a test mutation API to either Worker. Public-shaped HTTP requests
with request-bound internal assertions verified:

1. Owner and shared frame-set reads return their expected survivors.
2. The Python content proxy returns the exact seven fixture bytes from real R2.
3. Sharing withdrawal denies the old shared URL (404), retaining owner access.
4. Account disable denies the old owner URL (404) and hides the frame set;
   re-enable restores access to the retained object.
5. Deleting the banner promotes the other approved frame and denies the deleted
   URL; deleting the whole set returns an empty set.

The local runtime exited normally after the probe and its owned processes were
closed. These seven-byte fixtures test exact transport, not JPEG validity,
canonicalization, privacy-model accuracy, hosted memory limits or client rendering.
The probe reaches Core directly; it does not qualify Edge routing or production.

Private evidence under `/Users/macstudio/.codex/eddy-production/`:
`screen-frame-views-focused-20260906.log`,
`screen-frame-views-full-20260906.log`,
`screen-frame-views-native-20260906.mjs`, and
`screen-frame-views-native-20260906-a/{result.json,runtime.log}`.
Only synthetic fixture credentials were used. No MiMo credential was copied into
this code, documentation or runtime probe.

Pinned formatting, whitespace validation and prompt byte-integrity checks passed.
All files changed by this surface are fork-owned. Whole-branch upstream-touch
validation is recorded with the local commit; no push, PR or merge was performed.
