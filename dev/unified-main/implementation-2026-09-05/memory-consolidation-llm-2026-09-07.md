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
`memory_kernel_consolidation_schema.py`; the formatter and provider use copies
of that same data. Existing component tests compare it to the upstream model
and reproduce the observed older-runtime emission. Schema snapshot SHA-256:
`341d2a62a9ee254f7bffe8d05e11e09be9b7ec81ec8cefbf33e3974420283321`.

Workers AI receives separate system and user messages. The original GPT-specific
cache-breakpoint metadata is not a Workers AI request parameter; the text and
message boundary remain identical. The default model is
`@cf/qwen/qwen3.8-27b`, configurable through
`WORKERS_AI_MEMORY_CONSOLIDATION_MODEL`. Its [documented context window](https://developers.cloudflare.com/workers-ai/models/qwen3.8-27b/)
is 262,144 tokens. The selected model's official API schema was read through
`GET /accounts/{account_id}/ai/models/schema?model=@cf/qwen/qwen3.8-27b`. It
requires the Chat Completions named wrapper
`response_format.json_schema = {name, schema}`, rather than a bare schema.
The adapter uses `max_completion_tokens=8192`, `n=1`, `reasoning_effort=low`,
temperature zero and a
90-second timeout. Only a single `stop` choice's assistant `message.content`
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

## Verification

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
  SHA-256: `00c62559806e598799600288ca4e8d1febf9a5e295a4a23ccdd2477492c28b07`. Current invocation source is
  byte-identical to the deployed staged module. Evidence is retained privately
  under `$CODEX_HOME/eddy-production/consolidation-qwen-evidence-20260907.json`
  and `consolidation-qwen-hosted-20260907-c/`.

Earlier attempts are also retained as failures: Llama 3.3 omitted required
promote fields and was rejected; the initial GPT-OSS adapter expected `response`
instead of the returned Chat Completions `choices` (not a model-capability
finding). Qwen run A hit an unpublished Worker-domain 404 before inference;
run B reached the model but exceeded the 90-second deadline and exposed the
runtime schema difference. Those inputs stayed pending without fabricated
routes, and all owned resources were cleaned up. The final selected version
uses the reviewed Chat Completions contract, frozen original schema and low
reasoning effort; it still needs duplicate-policy qualification.

## Remaining production work

The inference owner takes a hydrated context and a run identity; it does not
register a public or unleased background trigger. Duplicate-policy fidelity, candidate retrieval, retry
leases, scheduler wiring, token-window batch planning, recurrence handoff and
remaining reader/writer/outbox convergence must be completed before automatic
maintenance and full CF-4/CI-1 qualification. The shared default prompt and
upstream files remain unchanged. This work alone does not deploy Eddy production
Workers or establish the signed macOS app's production UI acceptance.
