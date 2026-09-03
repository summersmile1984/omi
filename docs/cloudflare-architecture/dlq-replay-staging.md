# Cloudflare Queue DLQ replay boundary

The Jobs Worker now has a bounded operator seam for Queue dead-letter
deliveries. Cloudflare does not expose a Queue read/list API to a Worker, so
the seam does not attempt to inspect `omi-cf-jobs-dlq-staging`. Instead, the
same Jobs Worker is configured as a consumer of that DLQ and records each
delivery in `cf_queue_dlq_messages` as it arrives.

Only the serialized, validated `JobMessage` envelope is retained. The body is
limited to 16,000 bytes and is stored with a SHA-256 digest; malformed or
oversized deliveries are indexed as `invalid` without their body. No replay
request accepts a caller-supplied job payload.

## Replay contract

`POST /internal/cf/jobs/dlq/replay` is an operator-only staging route. It
requires all of the following:

- `DLQ_REPLAY_STAGING_ENABLED=true`;
- the `secret-key` `ADMIN_KEY` header;
- a bounded `idempotency-key` header;
- `x-dlq-replay-timestamp` within five minutes; and
- `x-dlq-replay-signature`, an HMAC-SHA-256 signature using
  `DLQ_REPLAY_SIGNING_SECRET` over
  `<timestamp>\n<idempotency-key>\n<exact request bytes>`.

The request body is limited to 50 unique captured message ids:

```json
{ "message_ids": ["<dlq-message-id>"] }
```

The idempotency key is content-bound. Reusing it with another message set is a
`409`; retrying the same signed request returns the original replay receipt and
does not publish a second Queue message. Each message is claimed in D1 before
publishing and transitions to `replayed` only after the Queue producer accepts
the immutable envelope. A producer failure returns a partial/failed receipt
and leaves that message eligible for a later operator retry.

Sync deliveries are routed back to `SYNC_FRESH` or `SYNC_BACKFILL` from their
validated `payload.lane`; all other JobMessage kinds go to `JOBS`. Queue
delivery is still asynchronous, so `replayed` means “republished”, not
“completed”. Existing DLQ messages from before this consumer was deployed
cannot be listed or recovered by this Worker and require Cloudflare dashboard
or API export/inspection.

## Staging positive probe

Once an operator has confirmed that a disposable staging message was captured
by the DLQ consumer, the replay path can be exercised without putting a
payload on the command line:

```bash
cd deploy/cloudflare
CLOUDFLARE_EDGE_URL='https://omi-cf-edge-staging.<account>.workers.dev' \
CLOUDFLARE_DLQ_MESSAGE_ID='<captured-message-id>' \
CLOUDFLARE_DLQ_ADMIN_KEY='…' \
CLOUDFLARE_DLQ_REPLAY_SIGNING_SECRET='…' \
npm run dlq:positive-probe
```

The probe signs the exact `{ "message_ids": [...] }` bytes, sends one
message id through Edge to the operator replay boundary, and exits non-zero
unless the response is a successful one-message admission (`202`,
`status=completed`, `queuedCount=1`, `skippedCount=0`, `failedCount=0`). A
reused `--idempotency-key` may return the original `200` receipt and is still
accepted only when it contains the same positive counts. The tool consumes the
response but does not poll the job or claim that the asynchronous job
completed.

This command is deliberately staging-only: remote URLs must be HTTPS
`workers.dev` origins whose hostname contains `staging`; localhost HTTP is
allowed only for a local proxy/fixture harness. It does not create a DLQ
message, list the Queue, enable the gate, or accept an unknown message id as a
positive result. The gate remains `DLQ_REPLAY_STAGING_ENABLED=false` until the
operator has a controlled staging window and a real captured fixture.

The gate is `false` in staging by default. This boundary does not change any
product owner gate, does not expose payload data in responses, and does not
claim old Cloud Tasks/OIDC or production queue parity.
