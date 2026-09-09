# Cloudflare canonical memory privacy rules — 2026-09-07

The normal Core build now stages the original canonical lineage module and
three unchanged privacy function bodies from
`backend/database/memory_apply_store.py`: evidence scrubbing, memory-item
scrubbing and deletion outbox construction. The generated module contains no
Firestore client or database adapter. Release source identity includes both
upstream files, and the existing release identity test rejects their mutation.

`memory_privacy_plan.py` prepares an apply result from an authoritative control
snapshot and lineage. The original resolver includes incoming aliases, cycles
and shared missing survivors; tombstones remain in the retry inventory. The
planner clears semantic fields, document bodies, trigger actions, capture
provenance and evidence references through the original scrubbers. It increments
item revisions and builds delete-only projection/vector events. Its privacy
epoch uses a server-generated 256-bit nonce and sequence, without the old
content-derived head. The deletion operation carries IDs and revisions without
deleted text, evidence IDs or the previous head. Writer transitions do not
prevent preparing explicit deletion.

This result is not storage authority or a deletion acknowledgement. Public
DELETE handlers are unchanged. Their migration still requires transaction-time
account/destructive-operation admission, authoritative item/evidence checks,
opaque anti-resurrection receipts, cleanup of prior content-bearing history,
observed provider erasure and final removal of deterministic tombstone IDs.
Those steps must preserve retry inventory until external cleanup is complete.
Other writers must honor that same deletion authority before history/revert can
be enabled. This step changes no route inventory ownership or release status.

## Local verification

The 17 privacy cases exercise actual staged production functions: fact,
document and trigger scrubbing; content-independent operation/epoch identity;
all four writer modes; incoming aliases and cycles; missing survivors and
tombstone retries; foreign, duplicate, missing and invalid proposals. The
privacy and existing kernel suites passed 27 cases together:

```sh
cd deploy/cloudflare/python/api-core
uvx uv==0.12.3 run pytest -q --tb=short -x tests/test_memory_privacy_plan.py tests/test_memory_kernel.py
```

`env PATH=<pinned-node-22-bin>:$PATH bash deploy/cloudflare/ci/routes.sh`
passed inventory/manifest validation, typecheck, 987 Worker tests in 124 files,
832 Core tests and 150 AI tests. Core retains the existing Starlette/AnyIO
deprecation warning. Private log: `memory-privacy-rules-routes-20260907.log`.
The existing release-source suite passed all seven cases. Pinned Python
formatting, diff whitespace and the zero-upstream-touch check passed.

## Hosted Python execution

The owned `memory-privacy-hosted-20260907-a` run built Core through the normal
builder, froze and verified its uploaded module bytes, and exposed a private
test entrypoint. It had no D1, R2, Vectorize, queue or model bindings. All 21 HTTP
cases matched CPython results, including eight expected 400 denials. Calls
without the private probe key returned 403. Comparisons retained item and event
timestamps but excluded the control/operation wall-clock timestamps.

The run finished at `2026-09-07T04:02:25.336Z`. Its exact owned Worker version
and tag were checked before cleanup, and the Worker was observed absent after
deletion. The planner and all 11 generated kernel modules match the frozen
hosted bytes. Journal SHA-256:
`3f6a8f105d3273c267d848571cb015e904d909b3a4458dd23b4158a57027add0`.
Private evidence: `memory-privacy-plan-evidence-20260907.json` and
`memory-privacy-hosted-20260907-a/` under `$CODEX_HOME/eddy-production/`.

The four protected prompt/provider files still match `9b7e48dca2` byte for byte.
This hosted run proves native Python rule execution, not public deletion,
provider erasure, production deployment or the Eddy desktop client loop.
