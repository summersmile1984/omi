# Live Qwen recommendations on Cloudflare — 2026-09-08

Source revision: `e5623d42575f76f975d449515efae5b2675f5b5c`.

The actual Core and Jobs entrypoints were built with the ordinary pinned
builders, frozen, and checked against a second Wrangler dry-run. All 154 Core
source modules present in the payload match the working source byte-for-byte,
including `entry.py`, the recommendation provider, state and publication owners.
No handler, model response, prompt, clock or ASGI entry was substituted.
The fixture supplied signed synthetic owners and synthetic task content. It
does not prove public login or a macOS UI session.

One isolated App D1 received all 191 migrations through 0188. Two isolated
Workers used real service and Queue bindings and actual Workers AI. The Core
provider used its default `@cf/qwen/qwen3.8-27b`; no model override was supplied.
The older frozen release directory supplied resource/brand configuration only;
this run built current source and does not qualify that older release candidate.

## Executed business path

- Unauthorized recommendation reads returned 401; an unknown evaluation field
  returned 422. An empty other account received an empty projection without a
  model receipt.
- Native Candidate HTTP created a concrete action due in one hour. The original
  recommendation policy shortlisted it and the real model returned a valid
  selection, explanation and action. The public response referenced the same
  Candidate and a persisted intervention.
- The projection, intervention, evaluation, completed job and real usage receipt
  were persisted by the ordinary D1 publication transaction. The one successful
  call reported **388 input tokens and 826 output tokens**, with one job attempt.
- Re-reading returned the identical cached projection and did not increase model
  usage. Private debug reads required the debug header and the original owner;
  missing-header and other-owner reads returned 404.
- Public Later feedback suppressed the Candidate and changed the material
  version without another model call.
- Public acceptance created a readable task. The real Queue/Jobs consumer
  completed its no-default-integration receipt with one attempt. Other-owner
  task reads returned 404. This is not external task-service or Apple delivery.
- No Candidate write guard or Queue DLQ rows remained.

The two existing prompt-fidelity tests were executed on current source with
their original expectations: **3 parameterized cases passed in 1.91 seconds**.
They compare the staged recommendation constructor and consolidation schema /
format instructions with their actual upstream owners. Default prompts were
not edited. The full 1148 Core / 1028 Workers result belongs to the preceding
source commit; it was not rerun for this evidence-only documentation change.

Commands: Node 22 `/private/tmp/eddy-wmnow-hosted-20260908.mjs`; the ordinary Core
pytest runner selecting `test_memory_consolidation_llm.py::test_original_default_prompt_schema_and_sensitive_context_are_preserved`
and `test_recommendation_routes.py::test_projected_messages_match_execution_of_original_message_constructor`.
The hosted driver exited 0. Selected evidence and its driver are retained under
`~/.codex/eddy-production/canonical-qwen-wmnow-hosted-20260908/`.

## Owned runtime and cleanup

Core version: `7454e605-e984-4b1e-aacc-5e24a4e696ff`.
The journal records the Jobs version, all resource creation IDs, HTTP statuses,
model receipt, frozen file hashes, and subsequent absence observations.
Both Workers and the App D1 / two Queues were removed after checking their
recorded version/tag or creation ID. Cleanup finished at
**2026-09-07T21:43:12.005Z** (September 8, 05:43:12 Asia/Shanghai).
No Eddy production resources were changed.

This establishes one real Qwen recommendation-to-task path with caching and
feedback. It does not establish general model quality, live recurrence or
outcome processing, external integration delivery, public login, native device
acceptance, CF-4/CI-1 qualification, or production publication.
