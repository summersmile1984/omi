# Qwen screenshot privacy and storage verification — 2026-09-08

Source base: `db1038aa84`, plus the screenshot model change in this commit.
The ordinary Core builder and entrypoint were used, with the isolated writer's
ordinary entrypoint. All 223 Core module files (222 Python files), the writer
bundle and all 192 App migrations through 0189 were frozen and retained.
The current judge/adjudication bytes match the hosted modules.

## Provider contract

The Cloudflare target now uses `@cf/qwen/qwen3.8-27b` for screenshot privacy.
The [official model schema](https://raw.githubusercontent.com/cloudflare/cloudflare-docs/production/src/content/workers-ai-models/qwen3.8-27b.json)
and [model documentation](https://developers.cloudflare.com/workers-ai/models/qwen3.8-27b/)
describe vision input through chat messages and structured JSON output. The
actual production `model_input` function passes validation against the archived
official Messages schema.

The original `_PRIVACY_PROMPT`, `ScreenFrameJudgement`, purpose admission,
retention, capture window, metadata normalization and seven-frame selection
rules are unchanged. The prompt SHA-256 is
`d48afcadd763119ab7d20d47a7ca1a6e7bd7d882486cc4d9416deb6354a71a5c`.
Core uses a text part and a JPEG data URL, the original JSON schema, one choice
and a bounded completion. It accepts only a completed assistant response;
refusals, tool calls, reasoning-only and contradictory output cannot approve an
image. Reasoning text is never promoted into the final verdict.

The model selected by the CF adapter also supplies the approval and usage
identity. The writer accepts exactly that identity and rejects an approval
naming the retired Gemini model before R2 work. The upstream Server policy file
retains its original provider configuration. No fallback to another model or
prompt alteration was introduced. Qwen completion token totals already include
reasoning and are counted once.

## Hosted business evidence

The owned run used real Cloudflare Images, Workers AI, D1 and R2. The Core
version was `21d56487-e821-4ea4-baeb-45c51ff32518`; the writer version was
`34cc61a4-da54-4f78-8dd4-4e7af77dd214`. The writer had no public workers.dev
route and received its calls through the service binding.

Two synthetic signed owners and completed conversations were seeded. No frame,
approval, verdict or successful attempt was seeded. A single request submitted
two 1600×900 PNGs: a meeting release presentation and a synthetic credential
vault. They contained no real account credentials or personal information.

| Exercised behavior | Observed result |
| --- | --- |
| Missing authentication | 401; no inference |
| Two-image adjudication | 200, one committed meeting frame, no credential image stored |
| Actual Workers AI usage | Two Qwen calls; 4,344 input and 745 output tokens |
| Identical attempt replay | Identical stored response; usage remained at two calls |
| Image and thumbnail reads | 63,080-byte JPEG and 9,679-byte thumbnail; hashes matched the D1 write receipt |
| Separate owner with the same conversation ID | Empty owner frame set |
| Anonymous shared view | Returned the shared frame and readable capability |
| Screenshot sharing revoked | Previously issued shared URL returned 404; owner URL remained readable |
| Global screenshot setting disabled/enabled | Old owner URL returned 404, then became readable again |
| Single-frame deletion and whole-set deletion | Deleted image URL returned 404; empty set returned successfully |

The stored meeting image was visually inspected. Decoded mean absolute RGB
error versus the two fixtures was approximately 0.904 and 11.169 respectively,
confirming which image was retained. The public response intentionally does not
expose each rejected candidate's verdict or failure reason. This proves the
credential image was omitted after two real model calls; it does not by itself
distinguish a semantic rejection from a candidate-local judge failure. Two
fixtures are not a comprehensive privacy model-quality evaluation.

A separate host-encoder byte comparison was unequal and is not used as a pass.
This run proves equality between the hosted receipt and the served bytes, not
byte-for-byte equivalence between different native/Pyodide encoder builds. The
previous same-runtime codec comparisons remain recorded in the
[hosted image evidence](screen-image-hosted-2026-09-06.md).

Business assertions passed at **2026-09-08T03:00:04.527Z**. The initial driver
then exited 1 because it closed its local command runner before invoking the
R2 cleanup CLI. This was a test driver cleanup error, after business success.
After confirming the original process was terminal, the recovery re-observed
both exact Worker versions/tags and all three resource IDs. It found the
expected D1 cleanup receipt, removed the two owned test objects, then removed
the Workers, Queue, bucket and D1. All were observed absent by
**2026-09-08T03:02:23.898Z**. The recovery exited 0. No code was redeployed and
no model call was repeated. Both temporary secret files were deleted.

## Local checks and retained evidence

- Core focused: `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests/test_screen_frame_adjudication.py` — **36 passed**, 29.57 seconds.
- Full Core with the same runner and `deploy/cloudflare/python/api-core/tests` — **1193 passed**, one dependency deprecation warning, 624.12 seconds.
- Focused Workers: Node 22 `node_modules/vitest/vitest.mjs run tests/screen-frame-writer.test.ts tests/screen-frame-r2.test.mjs tests/screen-frame-source.test.mjs` — **28 passed**, including actual local workerd D1/R2 and unchanged upstream prompt execution.
- Full Workers: Node 22 `node_modules/vitest/vitest.mjs run` — **1032 passed**, 128 files, 18.77 seconds.
- Node 22 `node_modules/typescript/bin/tsc --noEmit`, `scripts/validate-manifests.mjs`, and the scoped Python formatter passed.
- The rewritten transport expectations are grounded in the official Qwen schema above. Existing privacy/selection expectations were preserved. New HTTP regressions exercise truncated, refused, ambiguous, tool and reasoning-only responses against production processing; a signed retired-model approval is rejected by the actual writer.

Archive:
`/Users/macstudio/.codex/eddy-production/screen-frame-qwen-hosted-20260908/`.
It contains the frozen build trees, migrations, inputs, served image, official
schema, driver, terminal failure, recovery, journals and local test logs.
`archive-sha256.json` identifies retained files; no live secrets are retained.

## Remaining delivery scope

The eight screenshot inventory slots remain blocked. This run did not exercise
public Auth/Edge or native macOS capture, the full eight-large-candidate request,
automated hosted R2 cleanup/account erasure, or the two-target product contract.
Physical removal in this run was operator cleanup of disposable test resources,
not evidence of a successful background erasure workflow. The normal Core/writer
business path is now proven with the selected hosted model; complete CF-4 and
production deployment qualification remain outstanding. `release_qualified`
remains false.
