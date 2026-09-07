# Canonical Candidate external integration — 2026-09-08

Base revision: `8966e1f618a4f99dd706215f1361d915d43e3031`.

## Behavior and authoritative owners

Acceptance already committed canonical tasks/workstreams and the integration
outbox atomically. Its new post-commit dispatcher and public drain now claim
actual D1 leases and enqueue JOBS messages. The drain returns the number actually
queued, preserves the original generation header and 1–500 query limit, and does
not promise that external delivery has completed. Missing queue infrastructure
cannot roll back an already accepted task. Cron recovers lost hints/expired leases.

Core owns generation, lease, one dispatch start per lease, export metadata and
settlement; Jobs owns credentials and external network requests. The existing
Candidate guard batch checks task/outbox snapshots and deletion fences. Cloud
success writes export state and completed delivery together. Concurrent task
edits retry the complete snapshot without replacing the new description.
Repeated deliveries with one lease cannot issue two provider requests, and old
tokens cannot settle a newer attempt. Account generation changes/erasure prevent
stale export writes. Malformed ready rows are parked independently of other work.

The ordinary projector stages the original `utils/durable_queue_policy.py` and
`CANDIDATE_INTEGRATION_POLICY` from `database/candidate_integration_outbox.py`:
five failed attempts, 30-second exponential backoff, 1,800-second cap. Leases last
300 seconds as upstream. Cron eligibility honors not-before, with actual delivery
at the next five-minute scan. Transport retry/DLQ counters are separate.

Todoist, Asana, Google Tasks and ClickUp use the existing encrypted credential,
refresh and provider mapping owner. The common provider operation accepts a
normalized task; every in-tree caller migrated. Manual form validation stays at
500 title characters, while canonical task descriptions retain the original
4,096-character bound. Requests now use the upstream 10-second network deadline.
No new LLM invocation or default prompt change is involved.

Apple Reminders stages the original payload/tag functions from
`backend/utils/notifications.py`. Preparation persists sync_requested before
sending the original item and legacy fields in a silent push. Existing Firebase
credentials/token storage are reused. Data-only APNs uses content-available and
priority 5. Successful send means at least one device push was accepted, not that
the reminder was created: device `/v1/action-items/sync-batch` confirmation owns
exported state. This follows the [Firebase silent-push contract](https://firebase.google.com/docs/cloud-messaging/ios/receive-messages#handle_silent_push_notifications).
Missing credentials, no device, failed delivery and oversized data do not receive
false success. Apple background delivery itself is not guaranteed by the platform.

## Verification scope

Tests use actual public/internal HTTP handlers, original staged policy and all
App SQL migrations. Worker tests execute the actual Jobs queue, encrypted provider
adapters and Cron/DLQ registry with controlled external HTTP and Core bindings.
Core tests exercise cloud settlement rollback, accepted-task replay, a concurrent
task edit, original five-attempt backoff, lost queue hints, malformed data,
generation/erasure fences, signed encoded paths and Apple device confirmation.
The existing full workerd/D1/R2 suite remains the runtime schema guard.

Validation completed through the component's ordinary runners:

- `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests`: **1,138 passed** in 528.47s, including 10 new integration HTTP/SQL tests. The existing Starlette/AnyIO warning remains.
- Node 22 `node_modules/vitest/vitest.mjs run` in `deploy/cloudflare`: **1,028 tests / 128 files passed** in 16.76s, including 16 new Jobs/provider/FCM cases. Actual workerd migration/R2 tests are included.
- `tsc --noEmit`, `scripts/validate-manifests.mjs`, pinned Python formatting and `git diff --check` passed. Manifest totals remain 654 CF / 619 backend routes, 17 explicitly blocked routes and 30 staging resources.
- The ordinary `scripts/python-worker.mjs api-core deploy --dry-run` package is 14,720.14 KiB (gzip 3,830.76 KiB). Six changed runtime modules and both original integration policy/payload modules match their packaged bytes; the internal router is registered in the packaged entry.
- Jobs `wrangler deploy --config workers/jobs/wrangler.jsonc --dry-run` passed at 1,886.41 KiB (gzip 479.38 KiB). This base configuration is a staging template, not a production qualification candidate.

Logs and package identity are retained under the local immutable
`~/.codex/eddy-production/canonical-integrations-local-20260908/` evidence directory.
Read-only observation at `2026-09-07T20:42:44.103Z` (04:42:44 Asia/Shanghai on
September 8) still found all nine named Eddy production Workers absent.

## Remaining delivery requirements

This is local feature verification, not hosted Python, live provider/FCM, real
device or Eddy production acceptance. A provider success followed by a crash
before D1 settlement can be retried after lease expiry; the original providers
do not offer one universal idempotency contract. No global exactly-once claim is
made. Full workflow-control enablement, hosted product and dual-target release
qualification, production Worker deployment and production-connected macOS
acceptance remain required. No production resources, macOS artifact or model
defaults changed in this implementation; no push, PR or merge was performed.


## Entry-composition correction

The c66e9e3 package check found registration text but did not resolve the imported
router object. The new router alias was overwritten by the original integration
router import: nine original method/path pairs were mounted twice and the new
internal endpoint returned 404. The prior 1,138 module/HTTP fixture tests did not
exercise this actual composition. Their results remain historical local evidence,
not proof that the dispatcher was mounted. The distinct router binding and actual
`entry.app` regression are documented in the
[entry verification](canonical-integration-entry-2026-09-08.md); the corrected
source also passed real hosted Core/Jobs Queue delivery. The earlier immutable
evidence directory is retained unchanged for audit.
