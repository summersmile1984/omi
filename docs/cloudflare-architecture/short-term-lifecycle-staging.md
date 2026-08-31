# Short-term lifecycle staging authority

`POST /memory/admin/users/{uid}/short-term-lifecycle/run` is owned by the
Cloudflare Jobs Worker in the staging manifest. The route keeps the legacy
admin header (`secret-key`) and response envelope, but it never reads
Firestore or invokes the legacy backend.

The D1 authority is split into two layers:

- `0104_memory_lifecycle_projection.sql` adds the lifecycle fields required by
  the product memory contract to `cf_memories` (`status`, `processing_state`,
  `source_state`, `captured_at`, `expires_at`, evidence, revision, and account
  generation). Existing rows are backfilled from `valid_at`/`created_at` and
  older writers receive server-side defaults.
- `0102_memory_short_term_lifecycle.sql` stores the generation-bound control,
  run/idempotency record, and transition audit. A completed destination-bound
  account cutover can initialize a missing control row; an existing disabled
  or stale-generation row is never upgraded by the request.

The executor mirrors the legacy `short_term_lifecycle.v1` policy: it reads
bounded rows where `memory_tier=short_term`, `status=active`,
`processing_state=processed`, and `min(expires_at, captured_at + 48h)` is at or
before `evaluated_at`. A tombstoned/purged source produces
`source_tombstoned`; every other eligible row produces
`remain_short_term` with `requires_lifecycle_decision=true`. The executor only
writes transition audit rows and does not silently promote, archive, or hide a
memory.

Admission executes the bounded D1 run synchronously so successful calls retain
the legacy result shape. If a transient D1 failure occurs, the run is returned
to the Queue with a lease and retry timestamp. Queue redelivery reclaims an
expired lease, retries transient errors, and terminally acknowledges deletion
fences, malformed projections, or exhausted attempts. Transition inserts are
idempotent by the policy/evaluation/source fingerprint.

Before production cutover, run a staging probe that covers: one active expired
row and one tombstoned row, exact retry, a transient D1 retry, cross-account
scope, malformed lifecycle evidence, and account deletion residual cleanup.
Historical Firestore memory-item/transition backfill and production cutover
remain separate work.
