# Eddy: immutable memory-vector publication

Baseline: `b659d92a81`. The production projector could record the latest D1
revision while an older worker overwrote the external vector bytes: both writes
used the same ID. The reverse-completion regression failed on that baseline.

Migration 0163 and the memory publication owner now:

- Allocate immutable attempt-specific vector IDs and journal them before the
  external call. The journal contains identity/revision/cleanup metadata, not
  memory text or vector values.
- Compare canonical content, revision and account admission inside publication's
  D1 transaction. A canonical revision change revokes the previous mapping.
- Retract only the deletion task's observed revision. Cleanup works from retired
  artifact IDs, so a stale deletion cannot remove a new publication.
- Preserve uncertain writes for the full Queue/cron invocation bound. Successful
  receipts release the writer; exceptions and crashes do not.
- Keep external deletion pending until absence is observed after a previously
  observed vector or an exact processed delete receipt. Accepted mutations and
  local timestamps do not prove erasure. The account residual registry includes
  the journal, and account D1 purge waits until it is empty.
- Adopt pre-migration projection IDs with an old-writer drain window. Fenced
  cleanup can advance, but ownership changes, lease extensions and revival are
  denied. Blank legacy memory content retracts its projection.

## Verification

- `npm test` in `deploy/cloudflare`: **111 files / 895 tests passed**. The
  projector file contains 17 cases, including reverse completion, stale delete,
  account deletion during an external write, delayed deletion, lost responses,
  abandoned claims, fenced cleanup and the previous-schema upgrade.
- `uvx uv==0.12.3 run pytest -q` in `deploy/cloudflare/python/api-core`:
  **503 passed**, one existing Starlette/AnyIO deprecation warning.
- `git diff --check` passed. Correction from the hydration follow-up: the
  original typecheck failure was masked by the subsequent diff command's exit
  status. Two app test fixtures lacked the new memory-index methods. Those
  fixtures are now complete and a standalone `npm run typecheck` exits zero;
  the original claim that typecheck passed was incorrect.
- The actual source-mode Cloudflare product runner with `--run-core
  --run-recording` passed **13 core cases** and **15 recording/privacy cases**.
  This executes real local Auth, workerd, D1, WebSocket, Queue and R2. It includes
  public memory mutations, cascade deletion and two account erasures. Its model
  IO is controlled and it does not exercise hosted Vectorize. The new external
  publication races are exercised through production code, real SQLite
  transactions and controlled Vectorize IO in the Worker suite.

Private evidence lives under `/Users/macstudio/.codex/eddy-production/`:
`memory-publication-baseline-20260906.log` (expected failing reproduction),
`memory-publication-vitest-current-20260906.log`,
`memory-publication-core-final-20260906.log`, and
`memory-publication-cf-20260906-b/` (final local runtime). Earlier intermediate
fixture failures are not passing release evidence.

## Remaining qualification

This is a memory-publication prerequisite, not CF-4 or CI-1 completion. Hosted
Vectorize behavior and cleanup latency must be measured. An uncertain artifact
whose deletion receipt has already been overtaken remains retryable; no bounded
hosted cleanup time is claimed. Other projection families still use their
existing publication/deletion path. Freshness-aware hydration and accurate repair
diagnostics, canonical ledger history/revert, the remaining route families and
the full candidate qualifiers remain required. Eddy production and the native
client's production login/recording loop remain unverified.

Default prompts, model selections and extraction policies are unchanged. All
changes are fork-owned; no push, PR, merge or production deployment was made.

Protocol evidence: [Vectorize client API](https://developers.cloudflare.com/vectorize/reference/client-api/),
[Cloudflare's ordered async writes](https://blog.cloudflare.com/building-vectorize-a-distributed-vector-database-on-cloudflare-developer-platform/),
and [Queue/cron wall-clock limits](https://developers.cloudflare.com/queues/platform/limits/).
