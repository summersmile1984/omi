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
  `cf_task_llm_receipts` row only after a provider response is present. A
  provider failure is recorded as failed and returns 503; it is not replaced by
  an empty recommendation.
- Account deletion intent/tombstone checks fence every write, and all nine new
  D1 identity surfaces are registered in the account-deletion purge inventory.

This is a Cloudflare-owned contract for accounts whose cutover row is
`state=new`, `checkpoint_phase=completed`, and
`destination_backend_bound=1`. It does not backfill Firestore staged tasks or
reproduce the legacy executor/Redis history. Legacy accounts therefore remain
closed with `task_intelligence_unavailable` until their data is imported into
the D1 contract. Evaluation is synchronous for the released endpoint while
the D1 job lease provides retry/fencing state; a separate queue drain should
not be inferred from the presence of that row.

Verification:

```text
sqlite3 :memory: < deploy/cloudflare/migrations/app/0109_task_intelligence.sql
cd deploy/cloudflare && npm run typecheck
cd deploy/cloudflare && npm run validate:manifest
cd deploy/cloudflare/python/api-core && uvx uv==0.12.3 run pytest -q tests/test_task_intelligence_routes.py tests/test_entry.py
cd deploy/cloudflare && npm test -- --run tests/edge.test.ts tests/wrapped.test.ts
```

The focused Python suite covers authentication, candidate identity, device
scope, and D1 projection. The Edge suite covers all 13 route methods being
sent to API Core with cookies and caller-supplied assertions stripped.
