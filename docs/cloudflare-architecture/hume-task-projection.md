# Hume request identity projection (staging)

The legacy Hume callback resolves ownership by looking up a Firestore task with
`action=hume_mersure_user_expression` and `request_id=<provider job_id>`. The
Cloudflare callback never derives `uid` or `conversation_id` from provider
payloads. It first records an identity-neutral result in
`cf_hume_webhook_results`.

`0135_hume_task_projection.sql` adds the reviewed destination projection
`cf_hume_task_projections`. An operator may apply an attested plan through the
Jobs-only boundary (default gate is off):

```sh
curl -X POST "$EDGE_URL/internal/hume-task-projections/apply" \
  -H "content-type: application/json" \
  -H "secret-key: $ADMIN_KEY" \
  --data-binary @reviewed-hume-task-projection.json
```

The bounded plan must use `mode=reviewed-apply`, schema version `1`, source
`kind=firestore-task-export` plus an export SHA-256, and exactly one projection
containing the provider `request_id`, legacy task id/action, destination
`uid`/`conversation_id`, account generation, source-row SHA-256, review UUID,
and a SHA-256 over the canonical plan. Raw Firebase/Firestore credentials and
provider payloads are not accepted.

The D1 insert fence requires an existing destination conversation and a
completed Cloudflare account cutover at the same account generation. Account
deletion intents/tombstones reject the insert, the projection is immutable,
and the conversation foreign key lets the deletion owner purge it. Once the
projection is present, the Queue consumer may mark a completed Hume result as
`mapping_status=attested` and records the mapped identity columns. The mapping
update repeats the projection, conversation, cutover, generation, and deletion
checks in a D1 trigger. Duplicate apply and duplicate Queue delivery are
idempotent.

This seam does not write emotion rows, notifications, or legacy Firestore, and
does not change the exact provider callback route or declare production parity.
A real source export attestation and positive staging provider probe are still
required before any cutover decision.
