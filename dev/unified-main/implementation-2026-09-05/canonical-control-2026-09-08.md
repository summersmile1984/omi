# Universal workflow control and hosted Core/Jobs — 2026-09-08

## Source boundary

The fixed off/generation-zero response prevented released clients from selecting
canonical suggestions or Chat-first UI and from obtaining a migrated account's
write generation. The authoritative upstream `utils/task_intelligence/rollout.py`
grants task intelligence to every authenticated owner, independently of historical
workflow/UI flags. The ordinary memory/kernel projector now stages that pure
module by changing only its model import path.

The Core control endpoint reads the same `cf_account_cutover` generation and
deletion fences as Candidate writers, then calls the original rollout and
effective-control functions. No second control table or per-user rollout flag is
added. Unmigrated accounts remain generation zero; healthy accounts receive
workflow_mode=read and chat_first_ui=true. Storage failure or erasure retains the
original model's off/zero/false response with bounded fallback telemetry. Reads
do not create control metadata. Client-supplied generation/UI headers cannot
override the sampled result. Default prompts and model settings are unchanged.

## Actual mounted application verification

Tests now include `entry.app` and its signed-request middleware. They cover
unmigrated/current owners without writes, capability/generation sampling followed
by real create/accept requests, generation changes, old-request rejection,
deletion intents and tombstones, storage errors, owner isolation and client
override rejection. The related [entry collision fix](canonical-integration-entry-2026-09-08.md)
also adds one global mounted method/path uniqueness guard and real integration
prepare/settle reachability.

- Focused entry/control command: **12 passed in 6.54s**.
- Core ordinary full pytest suite: **1,148 passed, one existing Starlette/AnyIO deprecation warning, 537.68s**.
- Workers ordinary full Vitest suite: **1,028 passed / 128 files, 16.92s**, including actual workerd/D1/R2 coverage.

Commands: `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests`; Node 22 `node_modules/vitest/vitest.mjs run` from `deploy/cloudflare`. Both normal Core and Jobs builders and frozen-payload dry-run verification passed before hosted publication. Formatting, manifest and diff checks are retained with evidence.

## Hosted observations

The private verification driver built current unmodified Core/Jobs source through
the ordinary entrypoints, froze the modules, and verified a second dry-run's bytes.
It created one isolated App D1 and two Queue resources after proving name absence,
then applied all **191 migrations through 0188**. It deployed two owned Workers
tagged control-20260908-a. Business handlers, provider adapters and queue dispatch
were not replaced. No AI bindings, model calls or public-login test were involved;
the fixture signed synthetic principals using the original assertion producer.

Hosted HTTP proved unauthenticated rejection, original universal control at
generation zero, Candidate create/idempotency/accept/task read, original integration
status reachability, and the internal authority requirement. With no default
external task service, the actual Jobs consumer completed the original terminal
no-op branch. Queue completion was observed twice, once at generation zero and
once after the fixture advanced the authoritative D1 generation to one. Both
receipts had attempt_count=1. Old writes returned 409, another owner saw 404 for
the first Candidate, and an explicit erasure intent switched control off and
rejected mutation while the other owner retained capability. No Candidate guard
or DLQ rows remained. Generation/erasure changes were direct isolated D1 fixtures,
not proof of a public migration/deletion coordinator.

The hosted checks passed at **2026-09-07T21:15:50.339Z**. Cleanup checked each
Worker version/tag and each resource creation ID, then observed both Workers
and all three resources absent by **21:16:11.191Z** (05:16:11 Asia/Shanghai,
September 8). Frozen evidence is retained under
`~/.codex/eddy-production/canonical-control-hosted-20260908/`. Private credentials
and raw deploy logs are excluded. Production resources were not changed.

This verifies hosted Core/Jobs control and no-default integration delivery. Live
third-party task/FCM delivery, Qwen generation, public login, macOS acceptance,
remaining canonical writer/reader convergence and CF-4/CI-1 release qualification
remain required. No PR, push or merge occurred; production is not claimed ready.
