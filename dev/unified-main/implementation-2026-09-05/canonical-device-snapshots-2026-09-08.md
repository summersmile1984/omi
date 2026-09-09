# Canonical device snapshots — local integration, 2026-09-08

The original open-loop request contains no snapshot_id. The old Cloudflare
handler required that field and therefore rejected a valid desktop payload.
Both snapshot tables also used one row per device, causing independent
runtime/workstream snapshots to overwrite one another. The old writer did not
preserve request receipts or reject stale timestamps.

The new owner parses the original wire models, projects the unchanged upstream
window validator, and binds the device to the authenticated request headers.
Open loops require the same user's open canonical workstream; exact workstream
state and account generation are checked again inside the write transaction.
Context scopes include generation/device; open loops also include runtime and
workstream. Original stable snapshot/receipt identities and request hashes are
retained. Retries return the first receipt after a newer replacement; older or
changed same-timestamp snapshots conflict. Snapshots and receipts commit together.

Migration 0186 rebuilds the two existing tables while copying every old payload.
Malformed legacy payloads are retained rather than silently discarded. They remain
unusable as original-model input; new writes use the original bounded model.
It adds exact snapshot and receipt guards. Expired reads delete state and its
receipt with compare-and-swap; a newer concurrent snapshot survives. The original
50-receipt cleanup limit remains. Export returns owned business payloads, and
Jobs deletes the new receipt table without touching another account.

## Runtime findings

Public HTTP tests caught an existing-snapshot update failing the BEFORE trigger
when it inspected the generated scope column. The final trigger compares the
underlying new generation/device/runtime/workstream fields directly. Both context
and open-loop replacement are covered by behavioral tests.

The existing actual workerd/D1/R2 component test then failed during table rebuild:
`cf_candidate_guard_tasks` exceeded the runtime's maximum SQL expression depth of
100 when schema definitions were revalidated. The still-local 0183 draft now
balances all 33 original null-safe comparisons. None were removed. The same
workerd test subsequently executed the full migration chain and completed
write 201, content 200, post-deletion 404 and zero R2/receipt residual.
This is a runtime SQL-structure incompatibility, not missing D1 transactions.

## Scope of verification

Public FastAPI tests exercise original desktop payloads, multiple workstreams and
runtimes, exact retries after replacement, timestamp/key conflicts, window/shape
rejection, workstream closure during commit, generation change, transaction
rollback, portable export, expiry and a concurrent replacement before cleanup.
A real recommendation request after a context snapshot changes the original
facts and material version. The AI response and usage are controlled, so this
does not verify live Qwen or hosted Python Worker behavior.

The forward migration test copies legacy rows through all preceding migrations
and verifies their bytes survive, including malformed historical payloads.
The existing account-deletion test now seeds and purges snapshot receipts for one
owner while retaining another owner's rows. No production default prompt changed.

The fresh Cloudflare observation at `2026-09-07T18:46:07.363Z`
(2026-09-08 02:46:07 Asia/Shanghai) still found all nine Eddy production Workers
absent. The read used existing Wrangler authorization and changed no remote
resources. CF-4 and CI-1 remain unimplemented; no qualification was fabricated.

Commands and final results:

- Core full suite: `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests` — **1,103 passed**, one existing Starlette/AnyIO deprecation, 494.29s. Collection preceded the last concurrency case and balanced-SQL edit; the final Candidate/create/accept/resolve plus snapshot selection subsequently passed **56 tests, 40.31s**, including all **12 snapshot cases**.
- Initial snapshot/recommendation/export selection passed **26 tests, 17.45s**; the final standalone snapshot module passed **12 tests, 9.10s**.
- `node node_modules/vitest/vitest.mjs run` in `deploy/cloudflare` — **1,006 tests / 127 files passed, 16.81s** on the final SQL. The initial runtime failure is retained separately. The focused actual workerd/R2 plus deletion selection passed **24 tests, 9.68s**.
- `node node_modules/typescript/bin/tsc --noEmit` and `node scripts/validate-manifests.mjs` exited zero. The manifest still reports 654 CF routes, 619 backend routes, 17 tracked blocked routes and 30 staging resources.
- Pinned Python formatting and `git diff --check` pass. `node scripts/python-worker.mjs api-core deploy --dry-run --outdir /private/tmp/eddy-snapshot-core-bundle-20260908` exited zero: upload 14,689.68 KiB / gzip 3,822.40 KiB. Eighteen runtime files match current source bytes; 30 runtime/projected modules are hashed. Packaging is not a hosted Python execution result.
- Four protected prompt files still match `9b7e48dca2`; original attention instructions match HEAD.

The existing Eddy 0.1.0 (2026090701) macOS app and all three SVG assets are still
present. `codesign --verify --deep --strict --verbose=4` exited zero with full
system certificate access, confirming valid on-disk code and designated
requirements. The initial sandboxed verification failed and displayed unavailable
authority; no artifact was rebuilt or re-signed to obtain the successful check.
The artifact manifest still says notarized=false and service_verified=false.

Final test outputs, source hashes and the read-only observation are retained
separately at `~/.codex/eddy-production/canonical-device-snapshots-local-20260908/`.
The earlier recommendation snapshots are historical evidence and remain unchanged.

Outcome attribution, recurrence handoff, external integration execution, hosted
runtime/model verification, complete release qualification and Eddy macOS-to-
production acceptance remain required. Workflow control is closed, and migrations
0183–0186 have not been applied remotely. This is local implementation progress on
`codex/unified-delivery` based on `eefe1d6a62`, not completed production delivery.
