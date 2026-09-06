# Cloudflare realtime usage accounting

The existing `/v2/realtime/usage` route ignored the desktop client's stable
`turn_id`. Retries charged the daily counters again, did not participate in
shared desktop question/cost accounting, and could return success without D1.
Cached input was also priced as additional input instead of a subset.

Migration 0164 makes an immutable, UID-scoped normalized turn receipt own the
daily counters, desktop cost bucket and quota settlement. The shared quota
reservation and receipt insert commit in one D1 batch; a projection failure
rolls everything back. A retry retains the first accepted values across dates
and provider changes. Legacy clients without an ID retain one event per report.
Native speech aliases share a separate receipt identity and add neither an
external-model cost nor a desktop chat question. Existing duration accounting
continues to own native ASR usage.

Pricing follows upstream `utils.llm.realtime_usage.client_reported_turn` and
`client_reported_cost_usd`: fixed issued models, text-first cached input,
integer micro-USD half-up rounding. No default prompt, extraction instruction
or model selection changed. This route records client-reported metadata; it
does not prove a provider invoice or open a live model session.

## Verification

- Against the old handler, pricing regressions produced 5 failures and one
  pass; duplicate-date, atomic rollback and missing-storage cases all failed.
  The latter harness already included the new schema while exercising the
  old handler. Those results are not a clean-tree migration baseline.
- API AI: `uvx uv==0.12.3 run pytest -q`, **150 passed**, exit zero. Coverage
  includes concurrent Free-cap admission, retry at the cap, legacy account
  trial behavior, UID isolation, native aliases, immutable first-report values,
  deletion intent/tombstone fences, atomic rollback and historical upgrade.
- API Core: the same component command, **531 passed**, exit zero, with one
  existing Starlette/AnyIO deprecation warning.
- Cloudflare Workers: `npm test`, **111 files / 895 tests passed**, exit zero;
  standalone `npm run typecheck` exited zero. The first Worker run caught the
  new table's nonstandard deletion-fence declaration; standard insert/update
  fences were added without relaxing the guard.
- Actual local workerd runtime B, through Edge HTTP, D1 and existing Queues:
  **Core 14/14, recording/privacy 17/17, chat 11/11**, runner exit zero. The
  existing recording lane now sends four concurrent duplicate reports and a
  changed-value retry; one question and USD 0.000256 are recorded. It also
  checks native aliases, export and account isolation. Queue-driven account
  deletion removes both new receipts; the read-only storage inspector observes
  zero remaining account-owned App rows.
- Runtime A exposed a real export assembly omission after the accounting
  assertions passed: the query results were not included in the response.
  A new Core regression failed with `KeyError: realtime_turns` before the
  payload fix. Runtime B and the final Core suite include that fix.
- Pinned Python formatting and `git diff --check` pass. All changed files are
  fork-owned; no upstream source/tests or credentials are added. New tests run
  in the existing component suites and recording product lane.

Runtime command, with Node 22 and the existing Pyodide cache selected:

```bash
node deploy/cloudflare/contracts/local-target.mjs \
  --output /absolute/private/fresh-runtime-directory \
  --brand-id eddy-realtime-accounting --run-core --run-recording --run-chat
```

Private evidence under `/Users/macstudio/.codex/eddy-production/`:
`realtime-pricing-baseline-20260906.log`,
`realtime-accounting-baseline-20260906.log`,
`realtime-export-baseline-20260906.log`,
`realtime-accounting-ai-final2-20260906.log`,
`realtime-accounting-core-final-20260906.log`,
`realtime-accounting-worker-final-20260906.log`,
`realtime-accounting-typecheck-20260906.log`, and
`realtime-accounting-cf-20260906-{a,b}/`.

## Delivery boundary

All local runtime reports retain `release_qualified: false`. AI and Vectorize
IO are controlled; the Workers, HTTP, D1 and queue paths execute locally. No
OpenAI/Gemini inference or hosted Cloudflare deployment was performed here.
`POST /v2/realtime/session` still requires a complete native interactive
transport. The Cloudflare profile continues to deny direct external session
issuance. Route ownership counts, CF-4/CI-1 release requirements and production
gates are unchanged. The whole branch's existing upstream-touch over-budget
desktop documentation change remains outside this fix.
