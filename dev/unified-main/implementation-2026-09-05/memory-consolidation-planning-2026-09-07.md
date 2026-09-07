# Consolidation byte-aware batch planning — 2026-09-07

Failure-Class: FC-consolidation-retry-owner-incomplete

The previous runner claimed up to 20 sources, gathered their original context,
and rejected messages exceeding 110,000 bytes. It classified that fixed size
failure as a refundable dependency wait. A large batch therefore left a retry
row for every source; a single source with a large permitted candidate context
could defer forever without reaching upstream terminal review.

## Implemented boundary

`memory_consolidation_planning.py` renders the unchanged model messages, chooses
a fitting whole-source prefix and **regathers** each reduced context. Simply
slicing the existing context would omit projection readiness for processed
sources excluded from the smaller batch. A larger refreshed candidate context
causes another strictly smaller prefix; the original 20-source bound limits
planning. Feedback and per-source candidates retain the original limits/content.

The runner returns all unused IDs in their original order. Fresh unused claims
are removed only by their exact state/generation and unexpired owner, avoiding
artificial attempt-zero retry records. A prior attempt is refunded without
erasing its failure reason/budget. Measuring messages calls no consolidation
LLM; gathering context may repeat embedding/retrieval. Each dispatch invocation
still calls the consolidation model at most once. A single over-budget
source now spends bounded processing failures and reaches the original review
route after three failures, with **zero Qwen calls** and its source retained.

The 1,000,000-byte intake cap, 110,000-byte message cap, default prompts, original
upstream bounds and `@cf/qwen/qwen3.8-27b` are unchanged. This is byte admission,
not token counting or a guarantee of model context-window capacity. The retired
convenience invocation wrapper has no remaining production caller; tests compose
the real context and low-level invocation explicitly.

## Verification

Commands ran in the existing unified-delivery worktree:

```sh
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests
cd deploy/cloudflare
node node_modules/typescript/bin/tsc --noEmit
node scripts/validate-manifests.mjs
node node_modules/vitest/vitest.mjs run
```

- Core: **1024 passed**, one pre-existing Starlette deprecation warning, 334.22s.
- Workers: **1006 passed**, 127 files, 12.57s. Typecheck/manifests passed.
- Focused planning/read-dependency regressions after pinned formatting:
  **17 passed**, 9.68s. Existing context/retry/readiness/dispatch tests initially
  passed **37/37** before the additional regressions were added.
- Oversized Unicode batches traverse native intake → authoritative retrieval →
  original model formatter → runner → SQL apply → dispatcher continuation. The
  tests verify every source appears once, no unused retry row remains, candidate
  groups/feedback stay equal to the original context, and the measured system
  message SHA-256 remains
  `be2a82a77a5286d46b3af5b6320ebfbcbd2eb28bbeb76f82e637085c411bd9aa`.
- A single oversized source plus healthy later source drains through the actual
  dispatcher to public review; only the healthy source invokes synthetic model
  output. Readiness after shrinking, prior retry budgets and expired/replaced/
  generation-changed ownership are exercised through real SQL.
- Test-only replay of the actual old runner and LLM wrapper makes both size
  regressions fail. The first replay loaded a second exception class; the final
  replay shares its original class identity and reports `input_too_large`.
- The complete context path exposed the independent hidden-feedback write-guard
  bug, fixed separately in migration 0182. The actual old apply function still
  fails its new regression. See the [read-set record](memory-consolidation-read-set-2026-09-07.md).
- `scripts/backend-python-format`, `git diff --check` passed. Four protected
  default prompt files compare identically to `9b7e48dca2`; no upstream edits.

These tests use local SQLite for D1 SQL and synthetic model responses/usage.
They do not assert a live Qwen generation succeeded, hosted Pyodide/D1 execution
of this patch, or Eddy production readiness. Logs, old-code replay harness and
source hashes are retained under the local Eddy production evidence directory.

Still incomplete: recurrence-to-task ownership, other canonical writer/read
convergence, the 17 blocked backend routes, CF-4/CI-1 release qualification and
the signed macOS app's actual production-connected acceptance. The earlier
isolated hosted dispatcher replay is separate evidence, not this patch's cloud
verification. No production resources were changed in this turn.

## Hosted follow-up — 2026-09-08

The [isolated hosted trial](memory-consolidation-hosted-2026-09-08.md) passed eight
Unicode sources in batches of 3, 3 and 2 with no leftover attempt rows. It also
verified single-source overflow reaches review after three failures while healthy
work completes, using real Cloudflare bindings and controlled model outputs.
All temporary resources were removed. Production qualification remains separate.
