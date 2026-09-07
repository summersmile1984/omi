# Original consolidation messages over Workers AI — 2026-09-07

The production D1 apply adapter previously accepted controlled model decisions
but had no real inference owner. `consolidate_with_llm` now validates the supplied
owner/source snapshot, renders the unchanged upstream messages, invokes Workers
AI, validates the returned schema, and sends the decision to the existing atomic
normalization/route/graph/review transaction. Apply rehydrates after inference;
concurrent source changes or account deletion cannot be overwritten by an older
answer. Account/control admission is rechecked after context hydration and before
provider disclosure.

## Prompt and provider contract

The ordinary projector selects the original consolidation prompt, context
bounds/redaction, message constructor and cache-prefix sizing function from their
upstream owners. The Worker supplies invocation-local message containers and the
same schema reduction / format-instruction text as `langchain-core==1.3.3`, the
backend's pinned version. That small MIT-derived text retains its license notice.
It adds no LangChain runtime or tokenizer/vocabulary download. A golden prefix
was generated using the original constructor and real pinned LangChain parser;
the Worker formatter is tested against it byte-for-byte, including the full JSON
Schema, field descriptions, punctuation and whitespace. Its 10,491-byte SHA-256:
`be2a82a77a5286d46b3af5b6320ebfbcbd2eb28bbeb76f82e637085c411bd9aa`.

The first hosted Qwen observation measured a 10,469-byte prefix instead of
10,491: Pyodide's Pydantic 2.10 omitted `additionalProperties: true` for the
unconstrained arguments dictionary. The backend's Pydantic 2.11.10 and local
Core 2.13.5 emitted it. The original full schema is now frozen in
`deploy/cloudflare/contracts/consolidation-output-schema.json` and staged as
`memory_kernel_consolidation_schema.py`; the formatter uses a copy of that data. Existing component tests compare it to the upstream model
and reproduce the observed older-runtime emission. Schema snapshot SHA-256:
`341d2a62a9ee254f7bffe8d05e11e09be9b7ec81ec8cefbf33e3974420283321`.

Workers AI receives separate system and user messages. The original GPT-specific
cache-breakpoint metadata is not a Workers AI request parameter; the text and
message boundary remain identical. The default model is
`@cf/qwen/qwen3.8-27b`, configurable through
`WORKERS_AI_MEMORY_CONSOLIDATION_MODEL`. Its [documented context window](https://developers.cloudflare.com/workers-ai/models/qwen3.8-27b/)
is 262,144 tokens. The original `_invoke_consolidation_llm` calls ordinary
`invoke(messages)` and parses/validates the JSON reply afterwards. The CF
adapter now preserves that generation contract: it adds no `response_format`.
The original schema remains in the unchanged system prompt, and the original
Pydantic and business validators remain the write boundary.

Calls use `max_completion_tokens=8192`, `n=1`, `reasoning_effort=medium`,
temperature zero and a 180-second timeout. The deadline accommodates the
observed 111.50-second valid duplicate response; the earlier 90-second deadline
could not accept it. Only a single `stop` choice's assistant `message.content`
is parsed; refusal, truncation, extra choices or reasoning-only content fails
without applying decisions. Overrides must support this same wire contract.

The 110,000-byte input / 256,000-byte output bridge bounds are memory guards,
not proof that every 20-item candidate envelope fits the model's token window.
The maintenance caller still needs provider-aware batch sizing. Oversized or
provider-rejected context stays pending; this module never trims the default
prompt, invents an outcome, retries on its own or changes providers.

Actual reported input/output usage is recorded under the `memory_consolidation`
feature in the existing daily usage table. Missing/malformed usage fails the
operation instead of recording invented token counts. Inference already consumed
by a malformed output or stale-source rejection remains counted. No raw model
payload, source content or provider exception is added to telemetry.

## Generation contract correction — 2026-09-07

The previous CF adapter added provider JSON-schema mode even though the original
Server invocation did not. The fixed medium-reasoning replay with that parameter
returned a promote decision without required normalization fields. A paired
request preserved the entire saved request, removing only `response_format`;
its completed reply contained the required fields. This supports removing the
adapter-added mode and preserving the upstream generation contract. A related
[Qwen issue report](https://github.com/QwenLM/Qwen/issues/2329) describes
reasoning/final-output divergence with response_format on another provider;
it is corroborating context, not proof of Cloudflare internals.

A predeclared four-call set then ran the unchanged prompt over real Qwen REST
responses through current Core validation/apply and local SQL-backed native
routes: primary promote/reject passed, and three duplicate trials produced
**archive, promote, archive**. All outcomes were retained; the set **failed**.
Elapsed times were 35.81, 111.50, 53.22 and 32.63 seconds. Candidate IDs and their
0.8 scores were fixture-controlled. This proves neither hosted candidate
retrieval nor reliable duplicate policy; the semantic failure remains open.
No replacement decisions, prompt edits, retries or best-of selection were used.
The observed trial parameters are now the runtime invocation parameters.

The generation-contract regression failed twice before the code correction
(`2 failed, 17 passed`), because the actual AI payload contained response_format.
The full original-schema equality guard remains and now lives alongside prompt
fidelity checks, independently of provider generation options. The final complete
Core run passed **969 tests**, one existing Starlette deprecation warning, in
281.78 seconds. Log: `eddy-qwen-generation-core-20260907.log`. The ordinary
provider/SQL suite is still in the existing local/CI Core lane. Pinned formatting
and diff whitespace checks passed. No upstream source or default prompt changed. Private request/response and
journal evidence is under `consolidation-generation-20260907/`.

## Earlier hosted verification (commit 8d1a3a21fd)

The selected Qwen model is reachable and the primary memory path passed, but
**the duplicate-memory business qualification failed**. This is not a full
production qualification.

- Focused Core provider/SQL suite: **19 passed**. Complete Core suite:
  **969 passed**, one existing Starlette deprecation warning. Commands:
  `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python
  -m pytest -q --tb=short -p no:cacheprovider
  deploy/cloudflare/python/api-core/tests` and the same command with
  `deploy/cloudflare/python/api-core/tests/test_memory_consolidation_llm.py`.
  Logs: `eddy-consolidation-qwen-core-final-20260907.log` and
  `eddy-consolidation-qwen-focused-final-20260907.log` in private evidence.
- Tests cover the original prompt/schema including the observed older-Pydantic
  difference, sensitivity redaction, Chat Completions JSON/fenced JSON output,
  all four controlled real apply routes, usage, malformed/omitted/foreign
  decisions, incomplete-promote output, refusal/truncation/reasoning-only/extra
  choices, provider timeout, source races, byte bounds and deletion disclosure.
- Hosted `eddy-consolidation-live-20260907-qwen-c` used fresh ordinary Auth,
  Rate Limit, Core and Edge Workers and isolated Auth/App D1 databases with all
  current migrations. The private probe called the actual invocation/apply
  owner; production source had no test route or provider substitution.
- The first real Qwen call promoted the explicit daily jasmine-tea preference
  to Long-term and rejected the fictional TV character's preference while
  preserving its third-party subject. The actual native HTTP flow verified
  normalization receipts (input revision 1, output 2, final item revision 3),
  D1 graph assertion and the owner's public list containing only the preference.
- The second call supplied the same normalized text and the existing memory as
  a candidate. Qwen returned `promote/create` again despite recognizing the
  duplicate in its explanation. The original validator accepted that otherwise
  valid decision, and apply persisted it. The expected `archive/reject` and
  target-memory assertion failed. The test was not weakened, and neither a
  deterministic replacement decision nor a prompt edit was introduced.
  Candidate identity was fixture-controlled; this is not Vectorize retrieval
  qualification. Later processed-search, usage-table and other-owner assertions
  were not reached and are not claimed for this run.
- Both actual Cloudflare/Pyodide calls recorded the exact original system
  prefix: **10,491 bytes**, SHA-256 `be2a82a77a5286d46b3af5b6320ebfbcbd2eb28bbeb76f82e637085c411bd9aa`.
  Both returned completed assistant JSON with real reported token usage.
- Run finished at `2026-09-07T10:31:44.631Z` with failure status. All four owned
  Workers and both D1 databases were observed absent after cleanup. Result
  SHA-256: `00c62559806e598799600288ca4e8d1febf9a5e295a4a23ccdd2477492c28b07`. The invocation source at commit `8d1a3a21fd` is
  byte-identical to that deployed staged module. Evidence is retained privately
  under `$CODEX_HOME/eddy-production/consolidation-qwen-evidence-20260907.json`
  and `consolidation-qwen-hosted-20260907-c/`.

Earlier attempts are also retained as failures: Llama 3.3 omitted required
promote fields and was rejected; the initial GPT-OSS adapter expected `response`
instead of the returned Chat Completions `choices` (not a model-capability
finding). Qwen run A hit an unpublished Worker-domain 404 before inference;
run B reached the model but exceeded the 90-second deadline and exposed the
runtime schema difference. Those inputs stayed pending without fabricated
routes, and all owned resources were cleaned up. That earlier version used the named JSON Schema request, frozen original schema
and low reasoning effort. The generation-contract correction above supersedes
those invocation parameters; duplicate-policy qualification remains incomplete.

## Remaining production work

The inference owner takes a hydrated context and a run identity; it does not
register a public or unleased background trigger. Semantic duplicate-policy fidelity, index-visibility admission, retry
leases, scheduler wiring, token-window batch planning, recurrence handoff and
remaining reader/writer/outbox convergence must be completed before automatic
maintenance and full CF-4/CI-1 qualification. The shared default prompt and
upstream files remain unchanged. This work alone does not deploy Eddy production
Workers or establish the signed macOS app's production UI acceptance.

## Shared exact duplicate admission — 2026-09-07 follow-up

The failed ordinary-generation trial is now a permanent regression fixture in
`backend/fork/tests/fixtures/qwen_duplicate_create.json` (only synthetic source
and evidence IDs rebound). A shared pure admission rule rejects `promote/create`
when complete source content exactly equals a live Long-term candidate belonging
to the same UID and a known identical subject. It does not compare model-rewritten
output text, vector scores, prompt-truncated text or paraphrases. It does not
produce archive/reject decisions on the model's behalf.

Server retains candidate identity during the original gather call with a local
hydration binding, without global mutation, extra reads or additional prompt
fields. Its original validator/retry owner records the invalid attempt as
retryable and calls no apply operation. CF applies the identical rule after D1
hydration and before any batch mutation. An existing candidate with unknown
legacy subject provenance remains processable; identity is not invented.

Verification (private directory `consolidation-admission-20260907`):

- Existing backend `test.sh` with explicit admission/registry/seam/startup and
  unchanged upstream consolidation files: admission **11 passed**, registry
  **8 passed**, upstream **64 passed**. Seam/startup initially stopped because
  the local venv lacked the declared boto3/psycopg dependencies; after installing
  their existing pinned versions, rerunning just those files gave **4 + 27 passed**.
  The retry case uses StrictFirestore's read-before-write transaction fixture.
- Core full run: **969 passed, 1 failed**, one existing Starlette warning. The
  new case's failure was its incorrect assumption that pending intake is absent
  from the native public list. Existing read behavior deliberately remains;
  the assertion now compares the whole public response before/after rejection.
  Rerunning the complete changed apply file gave **28 passed**. No other runtime
  code changed after the full run. Commands use the existing Core venv and
  `-m pytest -q --tb=short -p no:cacheprovider`.
- Disabling only the new admission helper for the corrected regression produced
  the intended **DID NOT RAISE** failure; restoring the real helper passes. The
  test proves whole-batch non-mutation, source preservation and acceptance of a
  later model-supplied archive/duplicate route.
- Saved real Qwen response replay through native HTTP intake, the production
  inference parser/usage recorder and local SQL apply: rejected with
  `output_invalid:exact_duplicate_create`; business journal and pending source
  unchanged; consumed usage retained (3434 input / 1541 output, one call).
  Prompt SHA remains `be2a82a77a5286d46b3af5b6320ebfbcbd2eb28bbeb76f82e637085c411bd9aa`.
  This is saved-response replay, not a new hosted inference or production deploy.
  The first replay's incorrect public-list assumption is retained as a failed
  evidence file; the corrected run verifies unchanged public state.
- Pinned formatter, diff whitespace and upstream-touch checks pass (zero upstream
  files changed). The new Server test is in the existing startup lane; changes to
  the shared module/fixture also trigger the existing CF route/Core test lane.

This closes the observed exact-duplicate *write admission* defect. Semantic
paraphrase quality, real Vectorize retrieval and the CF durable retry/scheduler
remain unqualified; the earlier failed live trials stay failed evidence.

Candidate retrieval is now implemented and separately exercised with real hosted
BGE-M3/Vectorize/Qwen. See [the context trial](memory-consolidation-context-2026-09-07.md).
Its successful duplicate call waited for the asynchronous index to become visible;
this does not close the production scheduler/readiness/retry requirement.
