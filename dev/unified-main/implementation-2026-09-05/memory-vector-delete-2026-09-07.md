# Cloudflare memory vector deletion completion — 2026-09-07

The memory projection task previously removed its D1 outbox record when it
retracted serving mappings. That acknowledged the task while the artifact
owner could still have a live writer or retained external vector. Account
erasure already waited on the artifact owner, but individual projection tasks
did not share that completion boundary.

`retractMemoryVectors` now returns completion only after the existing artifact
owner has drained the memory's writes and observed external cleanup. Until
then, the exact revision/operation outbox remains durable and Queue delivery
retries. The typed cleanup scope always includes a UID and can include a
source/revision bound. Unrelated sources, foreign owners and newer publications
do not delay the task or lose their vectors. Existing global reconciliation and
account erasure use the same owner. No new table or external deletion adapter
was introduced.

Failure-Class: FC-split-mutation-authority

This extends the reusable publication/cleanup owner introduced in local commit
`00b920bb68`. The new behavioral regression fails against `7e7feb4da3`: the old
Queue handler acknowledges the pending deletion rather than issuing either of
the two expected retries. The test exercises the actual message handler, D1
transactions and controllable provider IO, not source-text assertions.

## Verification

The existing route lane passed inventory/manifest validation, typecheck,
989 Worker tests in 124 files, 832 Core tests and 150 AI tests:

```sh
env PATH=<pinned-node-22-bin>:$PATH bash deploy/cloudflare/ci/routes.sh
```

The projector suite has 19 cases. New coverage includes Queue retry/ack,
accepted-but-unapplied deletes, account/source isolation, unrelated live writers,
late publication and provider observation failure. Existing stale-revision and
blank/restricted-source cases now wait for external cleanup before expecting
their outbox to disappear. This follows the documented asynchronous Vectorize
contract; it does not weaken canonical eligibility or stale-result assertions.

Private logs: `memory-vector-delete-routes-20260907.log` and
`memory-vector-delete-baseline-20260907.log`. Pinned Node 22 was used. The Core
suite retains its existing Starlette/AnyIO deprecation warning.

## Hosted D1 and Vectorize

The owned `memory-vector-delete-hosted-20260907-a` run created one D1 database,
one 1024-dimensional cosine Vectorize index and one private Worker. All 176 App
migrations applied. Its private entrypoint imported the actual publication and
Queue message handlers; it supplied synthetic vectors without model inference.
The Worker payload was frozen and verified before deployment.

The production publication owner wrote three vectors and their D1 mappings:
the target, another memory for the same owner and the same memory ID belonging
to another owner. All three were observed in the external index before deletion.
A fourth journal row represented an unrelated live writer. Deleting the target
initially returned retry, preserved its outbox/artifact and removed its serving
mapping. Subsequent deliveries returned completion only after cleanup. The final
provider read found exactly the two retained vectors, no target vector, and the
unrelated live writer remained in D1. This observed run took 48,033 ms from the
first deletion call through final reconciliation; it is not a latency bound.

The run finished at `2026-09-07T04:16:41.011Z`. The exact owned Worker version
and tag were checked before deletion; Worker, D1 and Vectorize were observed
absent after cleanup. Both production source files still match the frozen
build's source hashes. Journal SHA-256:
`530c025d38495461f3c3690a64398795559ef924c8a60e7ec58f72c784aff9d0`.
Private evidence: `memory-vector-delete-evidence-20260907.json` and the owned
run directory under `$CODEX_HOME/eddy-production/`.

The public local Core suite passed 16 cases, including memory editing, isolation
and deletion, in `memory-vector-delete-local-20260907-b`. Its recording suite
stopped at an obsolete expectation that content correction immediately becomes
searchable again. Persisted data instead showed the pending Short-term state
required by upstream `update_canonical_memory_content`, introduced to native
Cloudflare editing by `190158f4d2`. The recording contract correction verifies
the new content and increasing revision through public export, then requires
the pending item to be absent from vector results. No model prompt or processing
admission is changed to satisfy the test.

The corrected recording run `memory-vector-delete-local-20260907-d` passed all
18 cases, including the actual Queue, edited-memory search denial, daily recap,
export, cascade deletion and both account erasures with a zero-residual scan.
Recap verification now precedes the explicit memory correction so it retains
its original processed-memory assertions; the correction then proves that
processing admission is revoked. The intermediate C run exposed that fixture
ordering conflict and is not passing full-suite evidence. The runner preserved
production quiescence/settling delays and closed its owned local processes.
Its provider IO is controlled; hosted Vectorize evidence comes from the
separate remote run above. The public Core and recording commands were:

```sh
env PATH=<pinned-node-22-bin>:$PATH CLOUDFLARE_PYODIDE_CACHE_DIR=<existing-cache> \
  node deploy/cloudflare/contracts/local-target.mjs --output <new-private-directory> \
  --brand-id eddy --run-core --run-recording
env PATH=<pinned-node-22-bin>:$PATH CLOUDFLARE_PYODIDE_CACHE_DIR=<existing-cache> \
  node deploy/cloudflare/contracts/local-target.mjs --output <another-new-private-directory> \
  --brand-id eddy --run-recording
```

The production observation at `2026-09-07T04:22:54.304Z` still found all nine
intended Eddy Workers absent. The four protected prompt/provider files still
match `9b7e48dca2` byte for byte. No production release or default-prompt change
is claimed by these tests.

## Remaining scope

This verifies the individual projection-task completion boundary. Public
canonical memory deletion still needs transaction admission, full semantic and
history cleanup, opaque anti-resurrection receipts and finalization after the
provider cleanup. Other projection families retain their separate outstanding
ownership work. Uncertain writes and busy-index cleanup latency require further
hosted qualification; this sample establishes neither a bound nor the complete
CF-4/CI-1 product contract. Eddy production and its desktop production client
loop remain unqualified.

Protocol basis: [Vectorize client API](https://developers.cloudflare.com/vectorize/reference/client-api/).
The upstream correction policy is
`backend/utils/memory/canonical_memory_adapter.py:update_canonical_memory_content`.
