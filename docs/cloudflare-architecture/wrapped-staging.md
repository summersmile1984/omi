# Wrapped staging boundary

`GET /v1/wrapped/{year}` and `POST /v1/wrapped/{year}/generate` are implemented by
the Cloudflare Jobs Worker for the supported legacy year `2025`.  The Edge signs
Better Auth context for Jobs; Jobs is the authority for admission, aggregation,
provider output, result state, and notification publication.

## Durable contract

- `0107_wrapped_jobs.sql` creates `cf_wrapped_jobs` with a uid/year primary key,
  deterministic request fingerprint, source fingerprint, account generation,
  queued/running/completed/failed status, lease, retry, and result JSON.
- Generation is admitted only for an account whose D1 cutover is `new`,
  `checkpoint_phase=completed`, and `destination_backend_bound=1`.  The account
  deletion intent/tombstone triggers fence writes and the deletion residual
  inventory includes the job table.
- The worker reads at most 10,000 completed, non-discarded D1 conversations and
  10,000 D1 action items in the UTC calendar year.  A larger or malformed source
  projection fails terminally instead of silently truncating the recap.  The
  source fingerprint binds the Queue payload to the snapshot used for the job.
- Deterministic statistics match the legacy response fields.  Insight fields are
  generated in one Workers AI call using a strict JSON schema and bounded input;
  malformed/empty provider output is a retryable failure and never replaced by
  fabricated fallback content.
- Completion and the shared `cf_notification_outbox` publication are committed
  in one D1 batch.  Notification source id `wrapped:{job_id}` makes retries
  idempotent and lets the existing FCM drain deliver `wrapped_ready`.

The legacy response shape remains synchronous HTTP `200` for the generation
admission response (`processing`, `done`, or `error`); generation itself is
durably asynchronous and status is read from the GET endpoint.  Cloudflare
Workers AI replaces the old `wrapped_analysis` executor.  Historical Firestore
Wrapped result backfill is intentionally not inferred: only accounts with a
completed destination-bound D1 cutover can use this owner.

## Historical result replay planner (default dry-run)

`scripts/wrapped-history-reconcile.mjs` provides the smallest safe historical
slice: replaying an already completed Firestore result into the Cloudflare
`cf_wrapped_jobs` authority. It does not rerun the legacy provider, read
Firestore/GCS, or claim that conversations and action items have been replayed.

The input is a bounded schema-v1 manifest with a Firestore collection marker,
an export SHA-256, and at most 5,000 rows. Every row must be the supported
`2025` completed state and carry an independently computed source snapshot
SHA-256, destination account generation, timestamps, and the bounded result
shape already accepted by the Worker. The planner emits a manifest SHA-256 and
per-row SHA-256, deduplicates identical `(uid, year)` rows, and blocks
conflicting duplicates. Results containing credential/token fields are
rejected; there is no plaintext-secret import or implicit encryption fallback.

The generated SQL is guarded by a completed, destination-bound
`cf_account_cutover` generation and both deletion fences. It only inserts a
completed snapshot with `ON CONFLICT DO NOTHING`; it never queues a provider
generation and cannot overwrite an existing result. No D1/R2/Firestore network
write occurs during planning:

```bash
node deploy/cloudflare/scripts/wrapped-history-reconcile.mjs \
  --input /path/to/wrapped-manifest.json
```

After an operator obtains a bounded D1 row export, `--verify --input plan.json
--actual rows.json` checks status, request/source/result checksums, account
generation, duplicate rows, and deletion-fenced absence. Missing source
attestation, destination generation, or provider-side parity remains a blocked
row rather than a fabricated import.

## Verification

`tests/wrapped.test.ts` covers D1 aggregation and structured result publication,
transient provider retry with the lease contract, notification idempotency, GET
status readback, and account-deletion fencing.  The route inventory marks both
legacy paths `staging-owned` with Jobs as the target runtime; production remains
unchanged until the historical backfill and client/provider parity review is
explicitly complete.
