# Developer ask delivery — 2026-09-06

The Cloudflare Developer API now owns `POST /v1/dev/user/ask`. The route keeps
the upstream question/limit/timezone request, ordered answer/source response,
Developer `conversations:read` scope and 25-per-key/hour policy. Its full
[contract](../../../docs/doc/developer/ForkCloudflareDeveloperAsk.mdx) describes
retrieval, provider failures and permission boundaries.

The backend inventory remains 619 unique identities: 600 staging-owned and 19
blocked. This is one migrated route, not a completed production qualification.
No upstream route was removed and no legacy backend was substituted.

## Implementation and evidence

- The ordinary Core build stages the original QA prompt, context formatter,
  wire models and legacy fact renderer from their upstream files. The release
  source identity includes those files. Four protected default-prompt files
  remain byte-identical to `9b7e48dca2`.
- Native BGE-M3 and two tenant-namespaced Vectorize indexes produce candidates;
  D1 mappings and owned conversation rows supply actual content. Transcript
  hits precede summary hits. Stale locked/discarded/foreign candidates never
  reach answer generation.
- D1 keys, scopes and content snapshots are checked before model disclosure
  and before response release. The tests mutate conversation permissions,
  content, keys, scopes and facts through controlled production seams.
- A successful native inference records its actual token counters in the
  existing `chat` feature usage ledger. No question, generated answer or new
  business row is stored. Missing or unresolved numeric citations withhold
  the answer; the route does not invent citations or retry paid inference.
- `WORKERS_AI_DEVELOPER_ASK_MODEL` selects the native
  `@cf/meta/llama-3.3-70b-instruct-fp8-fast` model, separately from integration
  classification. The default prompt is unchanged; `temperature` is zero.
- The adjacent malformed-limiter fix is separate local commit `3befabe212`.
  Explicit fail-closed callers receive 503 for an incomplete HTTP 200 reply.
  Existing callers retaining fail-open admission still do so. Actual local
  HTTP through the compiled Edge bundle returned 503 and forwarded zero
  requests to Core with the controlled malformed limiter. The shared telemetry
  records `invalid_response`, `none`, `exhausted` without user data.

The existing `deploy/cloudflare/ci/routes.sh` lane runs manifest/inventory
validation, TypeScript checking, Worker tests and both Python suites. New tests
are discovered by these existing runners; there is no separate unattended CI
probe. The private local evidence directory is
`/Users/macstudio/.codex/eddy-production/`.

## Hosted findings

The hosted diagnostic creates five isolated Workers, two fresh D1 databases and
two Vectorize indexes after observing each name absent. It applies all current
Auth/App migrations remotely, creates real Auth accounts and scoped Developer
keys through public endpoints, then uses synthetic D1 conversation records and
real native embeddings/index writes. Its private Edge wrapper only gates the
diagnostic origin and supplies synthetic indexing/readiness operations. The
business requests pass through the actual Edge, Core, Jobs and Auth code.
No production binding or data is used.

Earlier attempts exposed distinct issues and are not successful qualifications:

1. `developer-ask-hosted-20260906-a` encountered Cloudflare's stock
   workers.dev page-not-found response after initial readiness. Its resources
   were fully removed. Later runs retry only explicitly identified Cloudflare
   propagation pages on the read-only ask/readiness calls or GETs. The final
   diagnostic also requires repeated actual Auth/Core route readiness and can
   reconcile a failed signup by its unique synthetic email in Auth D1: an
   existing account is signed into, while an absent account retries the same
   unique identity. Unreconciled writes are not replayed.
2. Run `b` proved that hosted Python rejected `Asia/Shanghai` because there is
   no OS timezone database. `worker_timezone.py` now loads the existing pinned
   pytz TZif data into standard-library `ZoneInfo`. A regression also runs
   without any OS timezone search path and checks Shanghai plus New York DST.
3. Run `c` returned the correct date and citations and passed account isolation
   and lock checks, but the diagnostic expected 200 for key deletion instead
   of its actual 204 contract. It did not run the subsequent revoked-key ask.
4. Run `d` returned a factually correct answer without citations from the 3B
   integration model. This prompted the dedicated 70B model selection and
   explicit citation validation; the prompt and expected citation requirement
   were not weakened to make the run pass.
5. Run `e` stopped on another Cloudflare page-not-found response during signup
   before testing the newly selected model. Its owned resources were removed;
   the final diagnostic adds the route readiness and signup reconciliation
   described above.

Each run observes owned Worker versions/tags and resource identities again
before deleting them, then records absence. Frozen Core modules and compiled
Edge source are compared with current code. Differences solely in emitted
relative-path comments are checked by the pinned esbuild parser/emitter.

## Final verification

`developer-ask-hosted-20260906-f` completed successfully at
2026-09-06T11:35:14.378Z. All 55 HTTP assertions matched their expected statuses,
including 17 business assertions; the remainder covered route readiness and
native Vectorize indexing readiness. Two real 70B inference calls returned the
agreed October 17, 2026 date with valid citations. The current D1 usage ledger
recorded exactly two calls, 1242 input tokens and 46 output tokens.

The other authenticated account received no source records. Locking the source
conversations returned the upstream no-context response without incrementing
generative usage. Public key deletion returned 204; the subsequent ask with
that exact revoked credential returned 403. Invalid credentials/scope/timezone
cases also retained their expected 401/403/422 boundaries.

All five temporary Workers, both D1 databases and both Vectorize indexes were
observed absent after cleanup. The successful journal SHA-256 is
`27ed6321bf27681944ef633649b459ba60dce583ccfd432205596ff24f3e7ed6`. The private summary is
`developer-ask-hosted-final-20260906.json`. Seven current Core modules match the
frozen hosted module bytes exactly, including both generated domain modules.
The Edge emitted-code equivalence check uses pinned esbuild 0.28.1; its digest
is `38c4d742d4607a92e4e28de68a3e93966fffbbb060572530d9031242fd141d94`.

Final test evidence:

- `PATH=<Node22>:$PATH bash deploy/cloudflare/ci/routes.sh` passed inventory,
  manifest validation, typechecking, 119 Worker files / 961 tests, Core 685
  tests and AI 150 tests. Log: `developer-ask-routes-final-20260906.log`.
- After the model/citation boundary was added, `uvx uv==0.12.3 run pytest -q`
  in API Core passed all **687 tests**, including 19 Developer ask cases.
  Log: `developer-ask-core-citations-final-20260906.log`. Current coverage is
  **1798 passing tests** across the three suites; manifest validation was rerun
  successfully against the final model configuration.
- The exact upstream prompt is executed with a fixed clock and compared with
  the staged renderer. Protected prompt-file identity evidence is
  `developer-ask-prompt-identity-20260906.json`.
- `scripts/failure-class validate --base 200c8e64db --head 3befabe212
  --pr-body-file <limiter-fix-commit-body> --format text` passed for the separate
  limiter repair with `Failure-Class: none`.

The live data was synthetic; no real capture ingestion, production Worker or
macOS production business session is implied by this route qualification.

## Production and desktop boundary

The macOS app at
`/private/tmp/eddy-macos-production-20260905-b/Eddy.app` passed
`codesign --verify --deep --strict` again. It identifies as Eddy, version
0.1.0/build 2026090502, bundle `dev.summersmile1984.eddy.macos`. Its rendered
deployment configuration points to the Eddy production Edge/Auth/Web domains.
This verifies the signed artifact and configuration, not a live production
login/capture/memory workflow.

At 2026-09-06T11:20:53.186Z all nine named Eddy production Workers were still
absent. The required CF-4 runner
`deploy/cloudflare/contracts/qualify-product.mjs` and dual-target CI-1 runner
`contracts/deployment/qualify-dual-target.mjs` are still absent. The previous
full release candidate also predates these changes. Production publication and
the real macOS-to-production business exercise remain incomplete; no release
gate was bypassed and nothing was pushed or merged.
