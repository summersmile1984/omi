# Memory ANN readiness — 2026-09-07

## Contract and implementation

The prior hosted context trial found a real 28.2-second gap between accepting
publication and retrieving the existing memory. The new gate belongs to the
production context owner, before embedding or Qwen invocation. It returns
`ConsolidationIndexPending` while eligible existing sources lack a complete,
current, query-confirmed publication. Input remains pending; this is dependency
continuation, not an invalid-model attempt or a generated business decision.
The surrounding durable maintenance lease/retry/scheduler remains unfinished.

The existing artifact journal gains expected publication size and per-vector
query proof. A view recognizes only a complete mapping from one immutable
attempt, same owner/source/revision/model, with all writers finished and no
retired parts. Missing/legacy/partial publications use the existing outbox and
reconciler, preserving an equal-version retry's backoff. A context invocation
checks at most 20 unverified vectors and stores successful proofs for later
invocations. It fetches neither vector values nor source text for this check.

The memory index has an indexed string `publication_id`, containing only the
already opaque 64-character vector ID. Querying by that ID with its exact
metadata filter and the tenant namespace proves ANN visibility without depending
on a vector winning against 100 identical embeddings. The release adapter makes
this metadata index a required provisioning policy. Old vectors are republished;
creating a metadata index does not retroactively index their fields.

A separate `projection_sequence` on `cf_memory_apply_control` detects concurrent
mapping insert/update/delete between admission and candidate hydration. This
column is outside the canonical business JSON and does not invent a ledger
commit. The post-retrieval check cannot silently refresh a changed snapshot.
Deletion fences still permit mapping retraction, and proofs cannot revive a
retired vector or a deleted account. Existing canonical source/control checks
remain in place before model disclosure and apply. Default prompts and models
are unchanged.

## Evidence and limits

- An independent hosted 1,024-dimensional index held 128 identical synthetic
  vectors. Ordinary top-100 retrieval omitted some IDs; an exact publication
  filter retrieved an omitted ID with actual score 1. The test recorded empty
  read observations before indexing and confirmed the index absent after cleanup.
- Earlier probe attempts are retained as failures: an invalid 4-dimensional
  configuration, reuse of a just-deleted index name, and an incorrect multipart
  upload. The successful probe used a fresh name and the raw NDJSON transport
  shown by the installed Cloudflare SDK. No model was called by these probes.
- Focused Core tests: 17 passed. They exercise pending input → delayed query →
  resume → original parser/apply, a 21-vector publication over two bounded
  invocations, legacy/missing/partial publication repair and preserved retry
  backoff, malformed/unavailable proof, revoked publication, and mapping
  replacement during retrieval. The original context tests still cover foreign
  and stale provider IDs, negative owner feedback and complete long-source lookup.
- Complete TypeScript suite: 997 passed across 125 files; typecheck passed.
  The first full run exposed a positional INSERT in the account-deletion fixture;
  it now names the original control columns and exercises the new default field.
- Complete Core suite: **987 passed**, one existing Starlette deprecation
  warning, 301.75 seconds. Substituting only the original context builder from
  `da6a4dbe0e` makes the delayed-index regression fail with **DID NOT RAISE
  ConsolidationIndexPending**; the current production path passes.
- Complete API-AI suite: **151 passed**, 8.25 seconds.
- Manifest validation passed (654 CF routes, 619 backend inventory entries).
  These counts do not qualify the 17 still-blocked routes.

## Hosted business trial: admission passed, overall trial failed

A fresh deployment used five owned Workers (Auth, Rate Limit, Core, Jobs and
Edge), two D1 databases through migration 0179, the real BGE-M3/Qwen bindings,
and one 1,024-dimensional memory index. The actual release adapter created the
required metadata policy (CLI exit 0); it became observable about 46 seconds
later. No production Eddy service or data resource was changed.

The first Qwen call promoted the explicit jasmine-tea preference and rejected
the fictional character's preference. The actual Jobs publication returned at
`12:26:32.451Z`. Four subsequent calls entered the **production** readiness gate
and deferred before any embedding or inference; each observation verified that
the pending source, canonical ledger JSON and Qwen usage count were unchanged.
At `12:26:59.506Z` the context lookup succeeded with the existing source and a
real score of 1; D1 then reported one complete vector with `query_ready = 1`.
Thus the approximately 27-second publication lag was handled by the code.
The private driver still owns repeat invocation; automatic scheduling is absent.

The second Qwen call then returned `promote/create` despite describing the
matching candidate at score 1. The existing shared exact-duplicate validator
rejected it with `output_invalid:exact_duplicate_create` (HTTP 503 from the
private probe). This is a retained model failure, **not** a successful complete
business cycle. Its provider response reported 3,511 input / 1,529 output tokens.
No replacement Qwen sample was requested. Both invocation prefixes retained
SHA-256 `be2a82a77a5286d46b3af5b6320ebfbcbd2eb28bbeb76f82e637085c411bd9aa`.
The failure makes the missing original retry/terminal-review owner the next
required step, rather than changing the prompt or treating readiness as release
qualification.

All five Workers, both D1 databases and the Vectorize index were observed absent
after cleanup; the run finished `2026-09-07T12:28:23.275Z`. Journal SHA-256:
`48d850441a3a44b3b86e3ecd32f473d37bea8ceb2143117790fdd378ca23a2ed`.
The hosted readiness/context/invocation/apply source files and migration match
the current files byte-for-byte. The later `vector_search.py` edit only corrects
its metadata docstring; its AST excluding that docstring is identical.

Private evidence is retained under
`$CODEX_HOME/eddy-production/memory-index-readiness-20260907/`, including the
failed trial, successful bounded admission observations, raw synthetic model
outputs, independent index probes, source hashes and complete test logs.

A cached proof establishes that the immutable publication became queryable; it
does not make Vectorize a strongly consistent database. D1 remains the source of
truth, canonical hydration still rejects stale/ineligible provider matches, and
future source or publication changes invalidate the corresponding admission.
The private hosted driver still owns repeat invocation: this change alone does
not qualify an unattended maintenance cycle or the Eddy production deployment.

Primary references:
[Vectorize API](https://developers.cloudflare.com/vectorize/reference/client-api/),
[metadata filtering](https://developers.cloudflare.com/vectorize/reference/metadata-filtering/),
[write visibility](https://developers.cloudflare.com/vectorize/best-practices/insert-vectors/).
