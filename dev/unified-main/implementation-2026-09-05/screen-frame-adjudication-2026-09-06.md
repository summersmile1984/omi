# Core screenshot adjudication and atomic publication

Core now implements the screenshot candidate endpoint, completing its internal
admission → canonicalization → judgement → approval → storage → publication
pipeline. The eight upstream screenshot route slots remain blocked in the Edge
inventory. This is implementation and local-runtime progress, not a completed
Cloudflare production deployment.

## Contract and ownership

The ordinary build now stages eight upstream-owned domain modules. Newly staged
transport digest verification, request fingerprint and capture-window helpers
retain their source behavior. The backend screenshot router is included in the
release source digest; the existing `backend/routers/**` CI trigger already
covers changes to that owner. No upstream file was modified.

`screen_frame_adjudication.py` checks the explicit, default-off egress flag,
isolated signer, AI binding and writer readiness before model IO. It then checks
ownership, completed status, account setting, the original two-minute capture
slack and per-candidate size bound. Every candidate's digest is checked before
the first model call. Binary bytes are decoded again when needed instead of
retaining a second complete batch. Duplicate client IDs preserve the upstream
last-value byte lookup behavior.

The unchanged canonicalizer produces the JPEG sent to the judge. The unchanged
privacy prompt and Pydantic judgement schema are sent through Cloudflare's
`google/gemini-2.5-flash-lite` binding protocol. Codec errors, malformed or
contradictory verdicts, provider failures and rejected candidates authorize no
write. Approved candidates receive a short-lived HMAC capability binding both
image digests, metadata, owner, subject, attempt, epoch and policy/model identity.
Only the independent writer can put the matching bytes in screenshot R2.

The D1 attempt is the sole publication/replay owner. A batch acquires its response
against the current revision/epoch and privacy state, updates survivors and the
adjudication timestamp, and marks newly written evictions for cleanup. A failed
publication trigger rolls back the response too. Concurrent publications retry
against the latest survivors; privacy changes cancel pending publication. An
all-rejected pass stamps adjudication without a revision increase, as upstream
does. A writer failure returns 503 and leaves the attempt unfinished. The writer's
existing scheduler now removes at most 128 expired attempt rows per pass while
retaining live replay windows and owner isolation.

Usage records the existing `screen_frame_judge` feature in `cf_llm_usage_daily`.
No application prompt, ASR text-cleaning rule or memory-extraction policy changed.

## Verified locally

- New Core HTTP suite: **26 passed**. It uses the actual canonicalizer and all
  production App D1 migrations, with controlled AI and writer transports. Cases
  cover admission, all-digest-before-inference ordering, exact canonical-byte
  identity, replay, conflicting fingerprints, codec and judge failures, original
  metadata normalization, seven-frame cap, low-score strip behavior, writer
  failure, publication rollback and privacy changes both during judging and
  after writer completion. A competing full HTTP adjudication runs while another
  publication is paused; both new frames and the prior frame survive the retry.
- Focused Worker/source-identity suite: **3 files / 31 tests passed**, including
  bounded attempt expiry and unchanged staged helper behavior.
- Full `env PATH=<pinned Node 22 bin>:$PATH bash deploy/cloudflare/ci/routes.sh`:
  **6 inventory tests, 619 route slots, manifest/typecheck success, 114 Worker
  files / 923 tests, 592 Core tests and 150 AI tests passed**. The command exited
  zero. Core reported the existing Starlette/AnyIO deprecation warning.
- A separate private probe compiled the normal Core entry and production writer,
  applied every App D1 migration and started them in actual local workerd. Only
  a synthetic completed conversation was seeded; no frame, attempt, approval,
  survivor set or R2 object was pre-seeded. Two real PNG candidates flowed through
  the production endpoint. A controlled Google-shaped RPC provider checked the
  original prompt SHA and schema, approving one canonical-image hash and rejecting
  the other. The actual writer independently verified Core's approval and wrote
  real R2 objects. The public-shaped content proxy returned an **8,836-byte JPEG
  exactly matching the independently generated upstream canonical result**.
  Replay, another all-rejected attempt, invalid digest, sharing withdrawal,
  account disable and deletion revocation all passed. The owned runtime processes
  were closed normally.

The native probe proves transport, actual image transformation, publication and
storage behavior. Its inference is controlled; it does not prove real privacy
classification, hosted memory limits, Edge admission, Better Auth login or an
Eddy production deployment.

## Hosted observation and remaining work

An authenticated request to Cloudflare's documented account-level `/ai/run`
endpoint for `google/gemini-2.5-flash-lite` returned **HTTP 402, code 2021** in
about **1.92 seconds**: insufficient gateway balance, requiring gateway funds or
BYOK. This was a synthetic text access probe, not a user's screenshot. No money
was added, provider key installed, Worker deployed or production data migrated.
The response proves this model access was unavailable at observation time;
other Workers AI model access is a separate question.

The unchanged codec permits source images up to its existing 64-megapixel
boundary. Its full decode/copy behavior has not been shown to fit the hosted
Worker memory limit, and the full multi-candidate request envelope also needs
hosted resource qualification. Neither an ordinary small-PNG success nor the
upstream input bound proves that requirement. Edge routing and complete hosted
business qualification remain pending; no route state or release gate was
promoted by this change.

Private evidence under `/Users/macstudio/.codex/eddy-production/`:
`screen-frame-adjudication-focused-final-20260906.log`,
`screen-frame-adjudication-worker-focused-20260906.log`,
`screen-frame-adjudication-full-20260906.log`,
`screen-frame-adjudication-native-20260906.mjs`,
`screen-frame-adjudication-native-20260906-a/{result.json,runtime.log,received.jpg}`,
and `cf-gemini-access-20260906.json`. Real authentication tokens were captured
only in process memory and were not printed or written to these artifacts.

Protocol sources: [Cloudflare Gemini model binding](https://developers.cloudflare.com/ai/models/google/gemini-2.5-flash-lite/)
and [Google GenerateContent](https://ai.google.dev/api/generate-content).
