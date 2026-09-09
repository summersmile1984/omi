# Trigger feedback: canonical identity and Cloudflare adapter

This work implements the required user feedback route; it does not publish the
nine Eddy production Workers or qualify the whole JIT family. Default prompts
are unchanged. No LLM, ASR, TTS or embedding call is part of this route.

## Actual defect and ownership

The upstream `apply_canonical_trigger_feedback` built a patch containing feedback
arguments. `_apply_canonical_user_mutation` omitted those arguments from the
logical operation payload. The real apply kernel compared different digests and
rejected the valid request with `ApplyStatus.payload_mismatch`. The existing
lower-level receipt test supplied an already-correct, manually assembled
operation, so it missed the adapter boundary.

Commit `4651f6066b` repairs the shared Server owner and adds a behavioral
regression. It executes the unchanged production operation builder against the
real apply kernel with controlled storage seams. A negative control supplies
the prior committed source to that test; it fails with the original digest
mismatch. The corrected code commits one revision. This is not a live Server
or Firestore run.

Cloudflare stages upstream feedback models, pure action rules and the canonical
adapter's decision code. Only persistence calls move to `FeedbackStore`.
The typed feedback participant commits through `apply_user_memory_mutation`;
it does not become a second memory writer. Candidate read-set guards protect
the original planned-notification receipt and new feedback receipt. Existing
memory guards protect account/head/item observations in the same D1 batch.

Migration 0190 adds immutable content-free feedback storage and extends existing
Candidate admission. New receipt insertion requires the matching committed
ledger operation, resulting item revision/head and notification feedback link.
The original three retries, five feedback actions, changed-payload conflict,
32-entry local window and durable receipt replay remain intact. Explicit user
feedback is available while proactive rollout is disabled or killed.

## Local verification

- `BACKEND_UNIT_TEST_FILE_LIST=/private/tmp/eddy-feedback-backend-selection-20260908.txt bash backend/test.sh`: 46 memory-apply tests and 27 original trigger-contract tests pass.
- Actual Core ASGI entry, complete App SQL and feedback tests: 20 pass. These include five actions, current watchlist effects, hidden replay, disabled/killed rollout, auth/input rejection, foreign/stale/legacy authority, failed-final-write rollback, competing identical commit, privacy revocation, owner export and replay after 34 feedback receipts.
- The unchanged prior Server function fails the new operation-builder regression with `payload_mismatch`; the corrected version passes.
- Edge/Core route composition uses existing authenticated owner binding and the original `memories:modify` quota. Existing Jobs deletion-registry tests cover the new identity table and insert/update fences.

Full Core regression passes **1213 tests** in 656.67 seconds. TypeScript and
manifest validation pass. The Worker suite's initial fence-discovery failure
was corrected by using the existing `CREATE TRIGGER IF NOT EXISTS` declaration
contract in migration 0190; the 14 relevant Edge/deletion tests pass.

The first three hosted public runs (`eddy-jit-feedback-20260908-a`, `-b`, `-c`)
failed on normal feedback with HTTP 409. All of their temporary Workers, D1
and Queue resources were removed. A separate controlled-fixture diagnostic
identified `D1_ERROR: LIKE or GLOB pattern too complex: SQLITE_ERROR` during
the batch. It was deliberately diagnostic-only, with additional error detail
in a temporary source copy; its Worker, D1 and Queue were removed too.

The receipt insertion check used a 101-byte `LIKE` pattern containing the full
feedback digest. D1 permits only 50 bytes in a `LIKE`/`GLOB` pattern, while the
local SQLite default was much larger. The SQL now compares an exact prefix with
`substr` and equality. The shared migration-backed test database now sets
`SQLITE_LIMIT_LIKE_PATTERN_LENGTH=50`, so the existing success-path tests reject
the original faulty SQL. This is a documented database expression limit, not
absence of D1 transactions. See [Cloudflare's D1 limits](https://developers.cloudflare.com/d1/platform/limits/).

The original faulty SQL fails all five action success tests when the shared
database enforces the documented limit (negative control). The final ordinary
public run `eddy-jit-feedback-20260908-d` passes **90 HTTP checks** through real
Auth/Rate Limit/Edge/Core and D1, ending at `2026-09-08T05:02:00.294Z`:

- Real signup, session restore and JWT; unauthenticated, foreign-account and invalid-body rejection.
- All five actions, one revision/head advance, unchanged condition/prompt/content, current watchlist and owner export.
- Twelve concurrent identical requests: exactly one commit and eleven durable replays, all with the same receipt.
- Injected final receipt-write failure: item, head, journal, outbox, event and receipt all unchanged; the same request succeeds after removing the fault.
- Feedback still works when rollout is off or killed; disable replay works against the hidden target.
- Changed payload, second feedback identity for the same notification, stale revision and post-deletion replay are rejected.

The test's four Workers, two D1 databases and Queue were removed, with absence
observed for each. It makes zero provider calls and uses seeded trigger rows;
this is not native trigger-creation or queued account-erasure evidence. The
final uploaded Core route, store, shared mutation owner and entrypoint match
their repository bytes. The final full Core suite with the D1 limit passes
**1213 tests in 640.17 seconds** (one existing Starlette deprecation warning).

Retained evidence: `/Users/macstudio/.codex/eddy-production/jit-trigger-feedback-public-20260908/`.
Worker suite: **1051 tests pass** across 129 files. Earlier failed and diagnostic
runs are retained separately and do not count as acceptance.

The shared Server fix's scoped preflight passes the digest/identity, invariant,
format, import, Firestore and workflow checks, then fails the unrelated Windows
dead-code check on the already-tracked `deploymentProfiles.generated.ts`.
That file is unchanged by this repair; no full preflight pass is claimed.

## Remaining delivery work

Trigger fixtures are seeded canonical rows, not evidence of native trigger
creation. Ledger mirror/prompt migration and projection owners, common
Server/Cloudflare JIT contracts, native end-to-end operation and queued account
erasure remain required. The route-inventory classification stays blocked under
the existing family completion gate. Full CF-4 and CI-1 release executors are
still absent, so this evidence does not authorize a frozen production candidate.
