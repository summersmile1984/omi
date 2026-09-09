# Eddy: canonical memory-vector hydration

Behavioral baseline: `00b920bb68`. A stale projection revision still returned
the current memory with its old vector score. The first regression in
`test_memory_vector_hydration.py` failed on that implementation. Independent
mapping, content and revision reads also allowed a response to mix snapshots.

Native `/memory/vector/search`, Developer API, MCP and chat-tool memory search
now call one shared hydration owner. A single D1 query reads candidate ownership,
canonical memory content/revision, published model and artifact metadata, and
current lifecycle/access admission. Responses consume that snapshot directly;
the former ID-only memory hydration route is rejected and all in-tree callers
have migrated. The result limit follows access/freshness rejection and chunk
deduplication, so denied hits cannot hide later valid candidates.

Native diagnostics count actual reads, authoritative records and rejection
reasons. Decisions use the upstream gateway's `allowed`, `stale_projection`,
`stale_vector`, `missing_authoritative_item` and `access_denied` vocabulary.
Owned stale IDs become repair candidates. Repairs reuse the existing artifact
journal and projection outbox, recheck current canonical intent transactionally,
preserve a newer healthy publication, and send due work a post-commit Queue hint.
Pending repair records in the response describe actual D1 artifact records.
Unknown/foreign provider IDs cannot authorize deletion of another account's
vectors. Missing/stale observations emit bounded telemetry without user content.
Default prompts, extraction rules and selected models are unchanged.

## Verification

- Core: `uvx uv==0.12.3 run pytest -q` — **519 passed**, one existing
  Starlette/AnyIO deprecation warning. Sixteen new cases cover stale revisions,
  coherent concurrent reads, access/expiry/generation rejection, duplicate
  chunks, overfetch, repair/write races, rollback, missing sources, old attempts
  and account isolation. Existing native, Developer, MCP and tool cases all run
  with canonical revisions and the publication journal.
- Worker: `npm test` — **111 files / 895 passed**. A standalone
  `npm run typecheck` exits zero. The earlier missing test-double methods and
  typecheck-reporting error were separately corrected in `9ac7fc2ca3`.
- Actual local Cloudflare source-mode product run B: **13/13 core** and
  **16/16 recording/privacy**. The new public-HTTP case records, finalizes
  through Queue, searches its memory, edits content and requires a strictly
  newer vector revision with the new content. Cross-account search is denied.
  Existing account erasure now also drains the real D1 vector artifact journal.
- The new controlled memory-index/embedding IO is part of the existing
  `deploy/cloudflare/ci/product.sh` lane, not a separate on-demand verifier.
  Real workerd, Auth, D1, R2, WebSocket and Queue owners execute production code.
  The transient provider double proves neither hosted Vectorize behavior nor
  model quality or index durability across provider restarts.
- Pinned Python formatting, diff checks and the per-change upstream-touch
  checks pass. No upstream source/tests/manifests/locks or default prompts changed.

The earlier vector fixtures used arbitrary timestamp versions, and one used an
expired historical short-term memory. They now represent real publication
state; that historical case freezes the D1 clock inside the memory's lifetime.
These expected values follow `INV-MEM-2`, the source eligibility view and the
upstream `models/memory_search_gateway.py` contract, rather than treating stale
fixtures as valid product behavior. No upstream tests were edited.

Private evidence under `/Users/macstudio/.codex/eddy-production/`:
`memory-hydration-baseline-20260906.log` (expected failing reproduction),
`memory-hydration-core-final-20260906.log`,
`memory-hydration-vitest-final-20260906.log`,
`memory-hydration-typecheck-20260906.log`, and
`memory-hydration-cf-20260906-b/` (final runtime). Run A used the wrong PATCH
fixture body key and failed; B uses the existing `value` wire contract.

## Remaining work

This completes the memory hydration prerequisite, not the full CF-4 or CI-1
qualification. Hosted provider behavior/cleanup latency, equivalent publication
ownership for the other projection families, canonical ledger lineage/history/
revert and the remaining route families still need implementation or evidence.
The complete candidate qualifiers are still absent. Eddy production deployment
and the macOS client's production business loop remain unverified. No production
deployment, push, PR or merge was performed in this change.
