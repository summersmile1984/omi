# Canonical recurrence handoff — local integration, 2026-09-08

The pre-change CF memory adapter raised
`memory_recurrence_handoff_unavailable` whenever a valid consolidation batch
contained recurrence signals. No signal could enter the original workflow inbox,
and that batch could not commit its memory results. The source at `a6f68f5def`
contains this explicit rejection. This repair connects the existing upstream
handoff and consumer instead of dropping signals or inventing a substitute task.

The ordinary Worker builder now stages `recurrence_kernel.py` from upstream
`workstream_association.consume_recurrence_signal`, its threshold constants,
proposal constructor and stable identities. Only the workflow-generation read
and Candidate creation call become async adapters. The original rules remain:
unresolved, at least two occurrences, at least two distinct days and confidence
at least 0.7. The pending workstream proposal retains ownership confidence 0.5.
No default prompt or suggestion-visibility threshold changed.

Migration 0188 adds `cf_task_recurrence_inbox` and its exact Candidate transaction
snapshot. The original RecurrenceInboxReceipt freezes the first signal, including
when a later batch rewords the same stable loop. A completed receipt never
reopens. The Candidate transaction owner exposes its prepared guarded statements
so the memory owner includes them in the same D1 batch as memory results, journal
and lease settlement. No separate transaction implementation is introduced.
A late handoff failure rolls back memory; a concurrent first receipt defers the
whole apply, preserving the original proposal and allowing a fresh read.

After commit, only UID/receipt hints enter the existing JOBS queue. Failed hints
emit the shared bounded fallback shape, and the existing five-minute Cron
rediscovers pending receipts. Jobs signs an internal method/path/audience-bound
request to `POST /internal/task-intelligence/recurrence`. User assertions cannot
invoke it. Current-generation ownership and deletion fences apply again in Core.
The original Candidate owner makes creation idempotent when it commits before
the inbox completion receipt. A retry after such a failure reuses that Candidate.
This creates a pending proposal; task/workstream acceptance is a separate user
operation. The existing DLQ registry captures/replays the new job kind.

Owner export and the explicit deletion residual/purge inventories include the
inbox. Two-owner deletion tests remove the requested user's receipt and retain
the other user's receipt. Migration INSERT/UPDATE fences include both account
deletion markers; UPDATE also checks old and new identities.

Verification:

- Initial unchanged Candidate/create and consolidation/apply selection: **37 passed, 25.53s**.
- Public HTTP/SQL handoff selection: `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests/test_recurrence_inbox.py` — **12 passed, 10.18s**. It includes the actual memory dispatcher with controlled model output, watermark settlement, signed Jobs receipt consumption, public Candidate acceptance and workstream read, original thresholds, first-signal freezing, receipt-ack failure, memory rollback, a failed queue hint, generation/authority checks, export and concurrent handoff.
- Full Core: `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests` — **1,128 passed, 518.31s**, one existing Starlette/AnyIO deprecation warning.
- `node node_modules/vitest/vitest.mjs run` in `deploy/cloudflare` with Node 22 — **1,012 tests / 128 files passed, 16.95s**. New Queue tests exercise Jobs.queue, internal signatures, retries, Cron rediscovery, stale/deleted owners and DLQ capture. Existing workerd tests execute the complete migration chain and the real R2 writer; this verifies migration runtime compatibility, not hosted Python recurrence execution.
- TypeScript `tsc --noEmit`, manifest validation, pinned Python formatting and diff hygiene pass. The manifest still inventories 654 CF routes, 619 backend routes, 17 tracked blocked routes and 30 staging resources.
- Python Worker dry-run via `scripts/python-worker.mjs api-core deploy --dry-run` succeeds: **14,699.55 KiB / gzip 3,825.45 KiB**. Eight source runtime modules match packaged bytes, and recurrence_kernel.py matches the ordinary projector output.
- Jobs packaging via `node node_modules/wrangler/bin/wrangler.js deploy --config workers/jobs/wrangler.jsonc --dry-run --outdir /private/tmp/eddy-recurrence-jobs-bundle-20260908` succeeds using the repository's staging template. This is a build check, not a production configuration qualification.

Initial test issues were an isolated-test import path, an expected EvidenceRef
that included null fields omitted by the existing public Candidate envelope,
and the static deletion-fence check's required `IF NOT EXISTS` syntax. They were
corrected without changing original business rules or weakening the checker.
The actual full migration/runtime test passed the final SQL.

A fresh read-only Cloudflare observation at `2026-09-07T20:00:26.488Z`
(2026-09-08 04:00:26 Asia/Shanghai) found all nine Eddy production Workers absent.
No remote resources were changed. The current CF-4/CI-1 qualifier entry points
remain missing; no passing qualification has been invented.

Final source/evidence hashes are retained at
`~/.codex/eddy-production/canonical-recurrence-local-20260908/`. This work is local
on codex/unified-delivery; migrations 0183–0188 have not been applied to
production. Hosted recurrence/model verification, external integration execution,
remaining canonical business convergence, release qualification and actual Eddy
macOS-to-production acceptance remain required. Workflow control remains closed;
this repair did not change the signed macOS artifact or its release readiness.
