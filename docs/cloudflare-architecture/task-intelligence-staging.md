# Task intelligence staging boundary

The 13 legacy write paths in the staged-task/task-intelligence group are now
owned by API Core in the isolated Cloudflare staging profile. Edge authenticates
the Better Auth session and forwards a request-bound assertion; API Core never
forwards these requests to the legacy backend.

The Cloudflare contract is deliberately D1-native:

- `cf_task_candidates` is the immutable candidate identity. IDs and
  idempotency are bound to `uid` and `account_generation`.
- `cf_task_context_snapshots` and `cf_task_open_loop_snapshots` are keyed by
  the authenticated device binding (`X-App-Platform` plus
  `X-Device-Id-Hash`). A payload cannot select another device.
- Interventions, feedback, outcomes, and evaluation debug records are tenant
  and generation scoped. Attribution chains must be created in the same
  generation before an outcome can be recorded.
- Candidate promotion uses one D1 batch: the deterministic action-item ID and
  the terminal candidate resolution are committed together. Repeating the
  request cannot create another action item.
- Evaluation creates a durable job row, claims a lease, calls the Workers AI
  binding with bounded task text, and commits the projection plus an
  `cf_task_llm_receipts` row only after a provider response is present. The
  bounded provider input is stored in `cf_task_intelligence_jobs.input_json`,
  so a transient provider failure releases the lease back to `queued`, publishes
  a `task_intelligence_evaluate` Jobs message, and retries up to three attempts.
  The third failure is terminal `failed`; it is never replaced by an empty
  recommendation.
- Account deletion intent/tombstone checks fence every write, and all nine new
  D1 identity surfaces are registered in the account-deletion purge inventory.

This is a Cloudflare-owned contract for accounts whose cutover row is
`state=new`, `checkpoint_phase=completed`, and
`destination_backend_bound=1`. It does not backfill Firestore staged tasks or
reproduce the legacy executor/Redis history. Legacy accounts therefore remain
closed with `task_intelligence_unavailable` until their data is imported into
the D1 contract. The released endpoint still returns a synchronous projection
on success; provider failures are retried asynchronously by Jobs through a
signed API Core internal processor. The scheduled Jobs reconciler republishes
queued or expired leases when a Queue delivery is delayed or lost.

Verification:

```text
sqlite3 :memory: < deploy/cloudflare/migrations/app/0109_task_intelligence.sql
sqlite3 :memory: < deploy/cloudflare/migrations/app/0110_task_intelligence_queue.sql
cd deploy/cloudflare && npm run typecheck
cd deploy/cloudflare && npm run validate:manifest
cd deploy/cloudflare/python/api-core && uvx uv==0.12.3 run pytest -q tests/test_task_intelligence_*.py
cd deploy/cloudflare && npm test -- --run tests/task-intelligence.test.ts tests/edge.test.ts
```

The focused Python suite covers authentication, candidate identity, device
scope, and D1 projection. The Edge suite covers all 13 route methods being
sent to API Core with cookies and caller-supplied assertions stripped.
