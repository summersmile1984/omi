# Canonical history read on Cloudflare

`GET /v3/memories/ledger-history` now reaches Core through authenticated Edge.
The source projector stages the original history admission, `MemoryDB` schema,
canonical wire projector, public field filter and bounded page algorithm.
Only the provider iterator becomes asynchronous D1 IO; persistence constructors
and server metrics exporters are not part of this read-only stage.
Default prompts and AI providers are unchanged. This route invokes no model.

The scan reads the single `cf_memories` authority. It does not backfill data,
create a control row, fabricate a ledger receipt or use vector search as history.
Fresh empty accounts return an empty array without writes. Disabled/unknown JIT
rollout takes the original cheap empty response. Existing rows with unavailable
canonical control return 503, not a successful empty history.

The original policy includes explicitly rejected, closed and preserved legacy
ledger records. Hidden, tombstoned, source-purged, sensitive, locked and pending
records stay excluded. Physical deletion/lock/review authority remains effective
while old writers converge on the canonical journal. Responses keep the original
bare array, temporal/lineage fields, owner identity and normalized `memory_id`.
The original default list remains separate from history.

Rows are ordered by `updated_at DESC, id ASC`. The original limit is capped at
500, `offset + limit` may not exceed 5000, and a 501-row provider window is
explicitly truncated. D1 transfers at most 32 rows and 1,000,000 document bytes
per page (a single larger row is charged separately), with a 4,000,000-byte
aggregate scan budget. A consumed deadline, document or byte budget returns the
honest prefix with `X-Omi-List-Truncated: true`; storage/identity errors return
503. This byte budget is a Cloudflare adapter limit, not a claimed upstream limit.

After scanning, Core checks the account/head and re-reads the same bounded row
cohorts, retaining fingerprints rather than another full content copy. Changes
observed during this read fail closed. A final uncached JIT decision rejects a
new kill/account change. Reads make no business writes. These checks do not prove
full writer convergence or cross-request snapshot isolation.

## Verification

- Actual Core ASGI plus all App migrations: **28 focused tests pass**. Coverage
  includes original synchronous page-policy comparison, original wire fields,
  query validation, owner isolation, disabled/unknown/fresh accounts, privacy,
  locked records, 500/501 rows, concurrent record changes, storage/control
  failures and deadline/byte truncation. The real byte-cap test reads 41 large
  metadata records, verifies every transfer fits the page budget and receives
  an explicitly partial result. It does not replace the limit with a smaller
  test value.
- Existing trigger snapshot tests: **14 pass** after extracting their unchanged
  shared head query/fingerprint into `memory_read_authority.py`.
- Full Workers suite: **1052 tests pass** across 129 files. TypeScript and manifest
  checks pass. The added Edge case exercises owner binding and strips forged
  authentication fields for the new path. Frozen source identity also records
  the history page, budget, admission, belief and public-field policy owners.
- Full Core suite: **1240 pass**, one existing Starlette deprecation warning,
  in 674.48 seconds. Collection preceded the final large-metadata test; the
  final focused run passes all 28 history cases including that additional case.
- An intermediate Workers invocation used the repository root rather than the
  documented Cloudflare working directory, causing two identity-import CLI
  failures. The expanded source-identity test also exceeded its five-second
  timeout after redundant Git launches; it now checks each dependency hash and
  exercises the common invalidation once. The documented invocation passes all
  1052 cases. Failed logs are retained, not counted as acceptance.

A real public Cloudflare run, `eddy-memory-history-20260908-a`, passed **24 HTTP
checks**, ending at `2026-09-08T05:29:19.166Z`. It used real signup/session/JWT,
Auth, Rate Limit, Edge, Core and D1. Closed/rejected/legacy/current/hidden/locked
records, owner isolation, pagination errors, generation rejection, rollout kill,
501-row truncation and account-revoked credentials were exercised. The canonical
control row remained unchanged during history reads. All four temporary Workers,
two D1 databases and one Queue were deleted, with absence observed.

Canonical ledger fixtures were seeded after native intake. This proves the
history consumer; it is not native ledger creation, revert, AI inference, queued
account erasure or production release evidence. Frozen uploaded sources and local
formatting-only differences are retained separately in the verification archive.

Evidence: `/Users/macstudio/.codex/eddy-production/memory-history-public-20260908/`.

## Still required

The subsequent [restore implementation](memory-revert-2026-09-08.md) supplies the
append/close transaction, exact operation replay and standalone reopen receipt.
Its verification is separate from this history-read record. Full lineage producers,
JIT mirror/prompt migration projections, Server/Cloudflare common acceptance and
native production acceptance remain required. The family retains its blocked
inventory classification. CF-4 and CI-1 release executors remain absent; no Eddy
production Worker is published by this change.
