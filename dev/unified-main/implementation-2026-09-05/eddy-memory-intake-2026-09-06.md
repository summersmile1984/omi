# Cloudflare memory intake tier admission

While checking the remaining ledger history/revert work, the five ordinary
native, MCP and Developer single/batch creation paths were found to admit
explicit-user input directly to Long-term. Native creation also trusted the
client's `durability=long_term`. That contradicts `INV-MEM-4` and the current
upstream `canonical_memory_adapter._resolve_initial_tier_value`: ordinary
intake starts in Short-term even when the user asserts durability.

The fork handlers now persist Short-term for new intake. MCP duplicate intake
retains the existing canonical tier; historical Long-term rows are not
demoted. Existing D1 lifecycle defaults set capture/expiry, and the revision
triggers commit vector work atomically with the memory and usage. This changes
neither default prompts nor extraction instructions.

## Verification

- Baseline `c921dd078a`: the new full-migration ASGI contract fails at all five
  creation paths because the stored tier is Long-term. The other six cases
  pass. The initial test collection error was corrected before this baseline
  measurement; a collection error was not counted as the regression proof.
- Focused intake/native/MCP/Developer suite: 46 passed. The new cases execute
  production handlers and the entire App D1 migration chain. Only delegated
  authentication and category-model IO are controlled. They verify Short-term
  and 48-hour expiry, atomic revision/outbox state, transaction rollback with
  no usage/Queue side effects, and retention of existing Long-term state.
- The shared `contracts/deployment/core.py` now checks manual durability hints,
  batch creation and persisted Short-term tier on both targets. Server OS
  actual HTTP: 14/14 passed against the retained local MiMo environment.
  Cloudflare actual local workerd HTTP: 14/14 passed; recording/privacy: 16/16
  passed, including recording/finalization, search/edit/new-revision retrieval,
  export and queue-driven erasure. Both runners exited zero.
- The first complete Core run found one fixture-clock mismatch (529 passed,
  one failed): Python was frozen in 2023 while D1 used current time, correctly
  treating the newly Short-term memory as expired. The test now freezes both
  clocks at the same instant; its expected upsert behavior is unchanged.
- Complete Core rerun: **530 passed**, one existing Starlette/AnyIO deprecation
  warning, 93.52 seconds, exit zero.
- Pinned Python formatting and `git diff --check` pass. No upstream source or
  tests are modified. The HTTP case runs through the existing local/CI product
  entries, and the ASGI cases are discovered by the existing Core pytest suite.

Private evidence under `/Users/macstudio/.codex/eddy-production/`:
`memory-intake-baseline-20260906.log`, `memory-intake-focused-20260906.log`,
`memory-intake-core-20260906.log`, `memory-intake-core-final-20260906.log`,
`memory-intake-server-20260906-a/` and `memory-intake-cf-20260906-a/`.
No credentials or business response bodies are committed here.

## Remaining production work

This proves ordinary intake admission, not the complete memory lifecycle.
Cloudflare still needs one canonical consolidation owner, terminal route and
atomic promotion/graph receipts, plus append-only ledger producers, history and
exact-idempotent revert. Hosted Vectorize and the complete CF-4/CI-1 release
qualifiers also remain required. The controlled local AI/Vectorize IO does not
establish hosted inference quality or provider cleanup latency. No route was
reclassified as owned and no production deployment was performed by this fix.
