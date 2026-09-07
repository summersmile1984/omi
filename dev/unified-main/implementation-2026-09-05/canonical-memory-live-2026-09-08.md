# Automatic memory processing with live Qwen — 2026-09-08

Business source revision: `e5623d42575f76f975d449515efae5b2675f5b5c`.
The subsequent `f6db2f672c` commit changed recommendation documentation only.

The actual Core/Jobs entrypoints and all provider, processing and projection
owners ran without substitutions. Both ordinary builds passed frozen-payload
dry-run verification, and all 154 Core source modules in the deployed payload
match this source revision. The fixture created synthetic signed principals;
it submitted one native HTTP memory and only read business state afterwards.
It made no direct SQL business mutations, model-response substitutions, manual
consolidation calls, manual vector publication or queue kickoff.

## Real capture-to-retrieval result

An isolated App D1 received all 191 migrations through 0188. Core and Jobs used
actual Workers AI, a 1024-dimension cosine Vectorize index with its publication_id
metadata index, and an isolated Queue/DLQ. Jobs retained its ordinary
`*/5 * * * *` Cron schedule and unmodified scheduled handler. No schedule wrapper
restricted the maintenance calls. These are test resource bindings, not a full
production resource configuration.

The synthetic owner submitted a standing preference for Simplified Chinese
meeting summaries and project status reports through `POST /v3/memories`.
The initial public write created Short-term/pending state at revision 1, observed
at **2026-09-07T21:46:29.152Z**. No client field bypassed consolidation.

The normal scheduled/Queue path discovered this input. Actual
`@cf/qwen/qwen3.8-27b` produced a normalized preference and a structured graph
plan. The original validator and atomic apply owner committed processing and
promotion receipts, a graph assertion and Long-term/processed state at revision
3. The graph is marked ready and references the persisted assertion and ledger
commit. Completion was observed at **21:50:42.274Z**; the server's processing
receipt is timestamped **21:50:34.129999Z**.

The real usage owner recorded **one consolidation call**, **3327 input tokens**
and **927 output tokens**. There were no remaining source attempts. Default
system messages were unchanged; the current original-message/schema fidelity
tests passed all three parameterized cases in 1.91 seconds.

The existing projector used real `@cf/baai/bge-m3` and Vectorize. Public
`GET /memory/vector/search` initially returned no match, then returned the
same canonical memory ID at **21:51:30.169Z**, with one hydrated result and
`legacy_fallback_used=false`. This run therefore demonstrates eventual
retrieval, not immediate indexing or a latency SLO. The normal account scan
reached wake_sequence=handled_sequence=6 with no lease owner. Repeated reads
and maintenance did not add another consolidation call.

The owner's default list contained exactly this memory. The other synthetic
account's default list and vector search were empty. Unauthorized intake-family
reads returned 401. No memory apply guards or Queue DLQ records remained.

The read-only Wrangler tail attached after the initial Cron event, so no Cron
tail event is claimed. It captured two successful real Jobs Queue events with
batch sizes 1 and 2 and no exceptions. Automatic discovery is established by
the configured ordinary schedule, unmodified handlers, native intake, resulting
business receipts and the driver issuing no processing/Queue calls.

## Execution and cleanup

Node 22 `/private/tmp/eddy-memory-live-hosted-20260908.mjs` exited 0.
Core version: `6dbf3e12-6466-448b-8993-b32a3fc89488`.
Jobs version: `8eb65835-e06c-4afa-9024-2c844d168a92`.
Selected driver, configuration, model/graph receipt, retrieval response, source
hashes, safe Queue observations and cleanup evidence are retained at
`~/.codex/eddy-production/canonical-qwen-memory-hosted-20260908/`.
Private assertion keys and raw tail logs are excluded from that evidence set.

The complete path passed at **21:51:32.398Z**. Both Workers, the App D1, both
Queues and Vectorize index were removed after verifying recorded version/tag
or creation ID. All six were observed absent by **21:51:50.012Z** (September 8,
05:51:50 Asia/Shanghai). The temporary assertion-key files were deleted after
evidence archival. No Eddy production resource was modified.

This proves one real-model native capture, automatic consolidation, graph
publication and vector retrieval flow. It does not establish broad Qwen semantic
quality, duplicate/conflicting memory decisions, all canonical writers, public
login in this fixture, macOS UI acceptance, ASR/TTS, CF-4/CI-1 or production
publication. The earlier controlled large-batch/retry tests remain distinct.
The separate [live recommendation record](canonical-qwen-live-2026-09-08.md)
documents Qwen recommendations, feedback, cached usage and task acceptance.

Documentation validation: HTML parsing, both new relative evidence links,
manifest validation (17 remaining blocked routes), scoped preflight suggestion
and `git diff --check` passed. Automatic browser refresh was refused by the
in-app browser file-URL policy; no new rendered-page acceptance is claimed.
