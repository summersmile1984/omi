# Cloudflare consolidation context — 2026-09-07

## Scope and current result

Automatic candidate construction is implemented and exercised through real hosted
Workers AI/Vectorize/Jobs/Core/D1. **Production maintenance is not enabled or
qualified.** The callable entry takes UID, bounded source IDs and a caller-owned
run identity. Candidate IDs/scores are not accepted from that caller. The original
lower-level inference/apply owner remains usable with a validated context.

The context owner uses existing BGE-M3 embedding and Vectorize helpers, then the
canonical D1 vector hydrator. It searches the entire source through 3,500-character
windows with 350-character overlap (source bound 50,000 characters), merges by
vector ID/maximum real score, bounds ranked hydration to 100 vector IDs and keeps
up to eight candidates per source by default. It omits the anchor itself and
never treats provider metadata as authoritative content. Stale/foreign/ineligible
projections use the existing rejection and repair owner. Whole source and account
state are rechecked before LLM disclosure; failures leave input pending.

Negative feedback uses the original upstream eligibility and text-bound functions:
active or hidden, active source, explicit owner rejection, last 30 days, restricted
labels excluded, newest 24 rows scanned, maximum eight bounded examples, 180 chars
per example/1,600 total. Both collection and rehydration use the same normalized
text. Hidden records remain disallowed as pending items or ordinary candidates.
No default prompt, upstream file or provider model selection was changed.

## Verification

- Complete Core suite: **977 passed**, one existing Starlette deprecation warning,
  278.64 seconds. Command: `PYTHONDONTWRITEBYTECODE=1
  deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short
  -p no:cacheprovider deploy/cloudflare/python/api-core/tests`.
- Seven new context cases cover native intake → retrieval → model parser/apply,
  actual scores, stale/foreign candidates, hidden/bounded/restricted rejection
  feedback, a long-source tail match, provider failure, concurrent canonical
  content edit, and foreign source denial. The corrected focused run also uses
  `-W error::RuntimeWarning`. The first draft's synchronous fixture call inside
  an async callback was replaced with the actual async edit owner, with an exact
  authority-change assertion; that incidental runtime error is not coverage.
- Substituting only `_hydrate_context` from prior commit `2e8ec9e9a9` makes the
  hidden-feedback regression fail with `memory_consolidation_source_changed`.
  Current code passes. This demonstrates a behavioral guard rather than a source
  string assertion. Existing route/Core CI runs discover the new test file;
  upstream context/feedback source changes now trigger that same lane.

## Hosted trial and its limits

Run B used five owned Workers (Auth, Rate Limit, Core, Jobs publisher and Edge),
two fresh D1 databases with all current migrations, and one new 1,024-dimensional
cosine Vectorize index. The private probe invoked the actual Jobs
`processVectorProjection`/`publishMemoryVectors` functions and Core's new context
entry. BGE-M3 and Qwen calls used real Workers AI bindings. No candidate ID or
similarity score was supplied by the fixture.

1. Public native intake created an explicit user jasmine-tea preference and a
   fictional TV character's preference. Qwen promoted the former and rejected
   the latter; required-normalization receipts, graph assertion and public
   default reads passed.
2. Jobs generated/published one real vector for the promoted item. A separate
   native memory was explicitly rejected through the public review endpoint;
   the resulting owner-rejected example was observed in actual Core context.
3. The new duplicate source queried real Vectorize. Six observations were empty;
   the seventh found the existing memory with actual score **1**. Publication
   returned at `11:45:08.267Z`; first observed retrieval was `11:45:36.475Z`, about
   **28.2 seconds later**.
4. The second and final real Qwen call received the retrieved candidate and
   returned archive/duplicate targeting the existing memory. Default/search
   reads retained only the preference; the other account read no memories.
   The queried usage row recorded **2 calls, 7,642 input / 3,896 output tokens**.
5. Both model calls retained original system-prefix SHA-256
   `be2a82a77a5286d46b3af5b6320ebfbcbd2eb28bbeb76f82e637085c411bd9aa`.
   No prompt changes or retries of Qwen outputs were used in this fixed run.

Run A is retained as a failed trial: primary inference and Jobs publication
passed; the probe then dereferenced an absent empty candidate group and stopped
before a second Qwen call. Run B fixes only that probe's empty-result handling.
Each run observed all five owned Workers, both D1 databases and its Vectorize
index absent after cleanup. Run B finished `2026-09-07T11:46:43.130Z`.

**The visibility wait currently belongs to the private test probe.** The serving
entry can still receive an empty candidate set while a fresh publication is
indexing. Production must add bounded index-readiness admission and durable
continuation, alongside retry leases, token-window batch planning, scheduler
wiring and recurrence handoff. The successful waited trial is not proof of an
unattended production cycle. Provider schema/ordinary invocation docs:
[Vectorize API](https://developers.cloudflare.com/vectorize/reference/client-api/),
[BGE-M3](https://developers.cloudflare.com/workers-ai/models/bge-m3/).

Evidence is retained privately in
`$CODEX_HOME/eddy-production/consolidation-context-20260907/`, including both trial
journals, responses, publication receipts, source hashes and test logs. No Eddy
production Worker was deployed and the signed macOS app's production UI
acceptance remains outstanding.
