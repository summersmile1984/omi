# Canonical outcome attribution — local integration, 2026-09-08

The Cloudflare handler at `8eb57c72c6` checked only whether the owner had an
intervention/feedback chain. It did not execute upstream
`backend/database/task_recommendations.py::_outcome_matches_chain`, so a result
for an unrelated subject could be attributed to that chain. A local replay of
that exact old HTTP handler against schema 0186 returned 200 and persisted the
unrelated result. The reproducer used only synthetic local data.

The ordinary Worker source projector now retains the original relationship
function, replacing its four Firestore reads with async D1 reads. Candidate
results allow their task/workstream, task sources allow their linked workstream,
and related workstreams allow their artifacts. The six original outcome codes
must match the requested subject kind. Rich WMNow intervention feedback subjects
and feedback-only chains use the same policy. Default prompts are unchanged.

This endpoint records attribution of a client-reported outcome. As upstream,
it is not a command to complete a task or approve an artifact, and it does not
add a new prerequisite that the task already be marked completed. The server
supplies occurred_at; an extra client timestamp is rejected by OutcomeCreate.

`recommendation_outcomes.py` reads the owned chain and canonical dependencies
through CandidateTransaction. Migration 0187 adds exact outcome/artifact snapshot
checks; the existing intervention/feedback, Candidate and task guards cover the
other reads. The live generation, dependencies and receipt share one D1 batch.
A changed relationship retries the whole read/decision and rejects attribution
that is no longer permitted. A late failure leaves no outcome or guard record.

The original request-derived outcome ID and first server timestamp survive
replays, including after a different event. The physical fingerprint binds the
record identity so distinct keys with identical bodies no longer collide on the
legacy unique index. The original request hash remains private payload metadata.
Historical request-only receipts keep their bytes and original identity after
the forward migration. Export now returns owned outcome records without internal
hashes; the existing account-deletion workflow removes them for only that owner.

The public HTTP regression module exercises WMNow → feedback → Candidate accept
→ outcome, all three relationship source kinds, artifact membership, unrelated
subjects, kind/code mismatch, feedback-only sources, missing/foreign chains,
new principals with no control record, generation changes, changed task/artifact
relationships during commit, failed writes, key replay/conflict, owner export and
forward migration of a historical receipt. AI output in the WMNow case is
controlled; the outcome path itself makes no model call.

Validation commands and results:

- Old-owner reproduction: `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python /private/tmp/eddy-outcome-before-reproducer.py` — old public HTTP returned 200 and persisted an unrelated result on schema 0186. Current public HTTP regression rejects an unrelated same-owner canonical task with 409.
- Focused public HTTP: `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests/test_recommendation_outcomes.py` — **12 passed, 9.34s**. An initial fixture used a feedback action absent from the upstream enum; it was corrected to the existing `complete` action without changing application behavior.
- Full Core: `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests` — **1,116 passed, 508.56s**, with one existing Starlette/AnyIO deprecation warning.
- Workers: Node 22 `node node_modules/vitest/vitest.mjs run` in `deploy/cloudflare` — **1,006 tests / 127 files passed, 16.61s**, including actual workerd execution of the complete migration chain and account deletion with two-owner outcome fixtures.
- Node 22 TypeScript `tsc --noEmit` and `scripts/validate-manifests.mjs` exited zero. Manifest inventory remains 654 CF routes, 619 backend routes, 17 tracked blocked routes and 30 staging resources.
- Pinned Python formatter and `git diff --check` passed. `node scripts/python-worker.mjs api-core deploy --dry-run --outdir /private/tmp/eddy-outcome-core-bundle-20260908` exited zero: upload **14,689.00 KiB / gzip 3,822.04 KiB**. Four modified runtime modules match their packaged bytes, and packaged recommendation_kernel.py matches the ordinary projector output.

Final source and evidence hashes are retained at
`~/.codex/eddy-production/canonical-outcomes-local-20260908/`. Earlier evidence
snapshots remain unchanged. The repair declares
`FC-attribution-chain-presence-without-relationship`; its reusable guard is the
public HTTP regression module, executed by the existing Core component runner.
It exercises the existing transaction owner and does not create a parallel
business policy or add a standalone release check.

Migration 0187 is a local draft. This work has not been deployed, verified in a
hosted Python Worker, or accepted through Eddy macOS. Workflow control remains
closed. Recurrence handoff, external integration execution, remaining business
convergence, live Qwen verification and complete release qualification remain
required. No production Worker, default prompt, or macOS artifact was changed by
this repair.
