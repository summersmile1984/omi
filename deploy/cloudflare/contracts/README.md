# Disposable Cloudflare product contracts

When the common core suite fails, the CLI emits bounded failed-case IDs and
HTTP status comparisons in CI. Full assertion text and command logs remain in
the private fixture directory; response bodies and credentials are not echoed.

`consolidation-output-schema.json` is the original backend
`ConsolidationAgentBatch.model_json_schema()` captured with its pinned
Pydantic 2.11.10. The memory projector stages it as data for both the unchanged
prompt formatter. Qwen receives the original text request and its JSON reply
is validated afterwards, as in the upstream invocation. This avoids the observed
Pydantic 2.10 omission of `arguments.additionalProperties: true` rewriting the
system prompt. Core's existing local/CI suite checks schema equality against the
current upstream model and byte equality against the original LangChain 1.3.3
prefix. When upstream changes the schema, review and regenerate both the data
contract and the original-parser prefix fixture from that owner; do not edit
field descriptions or defaults independently here.

`bash deploy/cloudflare/ci/product.sh` is the fork manifest's **local and CI**
entry. Install the existing npm lock with `npm ci --prefix deploy/cloudflare`
and have `uvx` available; `scripts/python-worker.mjs` installs/checks pinned
workers-py and uv, consumes both committed Python locks and rejects lock drift.
The entry builds seven real application Workers and an inference-only Worker,
applies the actual Auth/App migrations into a new local state directory, starts
locked Wrangler/workerd, then executes:

- The same `contracts/deployment/core.py` identity/JIT/onboarding/calendar/email/CSAT/memory/tasks HTTP suite
  used by the Server OS runner, including manual-memory edits, visibility,
  review, read/dismiss/baseline persistence, account isolation and deletion.
- `recording.mjs`, separate public `/v4/listen` (Upgrade Bearer) and
  `/v4/web/listen` (first-frame JWT) PCM recording flows: authenticated
  capture, committed transcript reads, reconnect ownership, cross-user denial,
  explicit finalization through actual Queue/Jobs/Core, derived memories/tasks,
  memory-vector retrieval, canonical content edits and retrieval of a strictly
  newer published revision, using the real public API and Queue consumer,
  and denial after logout. A separate account's recording proves that cascade
  deletion retracts its derived memories/tasks while the owner's recording
  remains; the populated owner's whole-account erasure stays a separate case.
  The suite restores the real account session and submits
  concurrent out-of-order daily desktop counters through the public route,
  verifies their maxima through export, generates a recap from the actual
  recording/task/memory records and counters, checks citations and account
  isolation, reuses it without extra model spend, and regenerates it in place
  with cooldown enforcement. Export includes the recap and excludes its internal
  generation token. It exports profile/recording/memory/tasks,
  verifies the download filename against the configured brand ID,
  and checks cross-user isolation. The privacy
  path also exercises scanner-safe GET and repeated one-click POST on the public
  email unsubscribe route, using only the isolated fixture's private issuer key.
  It creates/exports the opt-out, verifies another account's isolation, waits for
  real queued erasure and then retries the retired token. No email is sent; token
  query values and issuer keys never enter the contract trace. The privacy
  path also submits and exports a private CSAT rating, proves account isolation,
  uploads and reads real R2 bytes with checksum rejection, deletes both
  a populated account and a just-registered account through public routes,
  waits for the actual Queue consumer and unchanged 60-second quiescence /
  30-second settling delays, then proves credential revocation and another
  account's retention. `local-privacy.mjs` opens only the fixture's SQLite stores
  read-only: all product identity columns in the application's deletion registry,
  Auth identity columns, and R2 object keys must reach zero. The short-lived App
  tombstone and durable Auth revocation fence remain by design. This checks
  logical R2 deletion, not physical disk reclamation or hosted Vectorize erasure.
  The same lane submits concurrent synthetic realtime usage reports through
  public HTTP, verifies one quota increment and cached-input cost, checks that
  native speech aliases do not charge a chat question, then exports and erases
  the actual D1 turn receipts. It does not open an OpenAI/Gemini session or
  call those providers; these are client-reported accounting contracts.
  The inspector never seeds state or runs a deletion processor. This is not the
  full dual-target recording matrix.
  The local provider includes deterministic embedding output and a transient
  memory-index RPC double. Production publication, D1 hydration and cleanup run
  unchanged against that controlled IO. The double proves neither hosted
  Vectorize semantics/latency nor index durability across provider restarts.
  Recap assertions run while the derived memory is processed. The later native
  content correction is verified through public export as a newer pending
  Short-term revision, which must be excluded from vector results until it is
  processed again; the contract does not fabricate that processing admission.
- `chat.mjs`, the current upstream Web `api.ts` get/send/clear functions against
  actual HTTP/SSE, Python model RPC and D1: configured brand greeting/default
  system prompt (via an inference-only echo), app-generator platform identity,
  goal-advice assistant labels with cross-user denial, usage/support presentation,
  task share/acceptance sender identity, preservation of user text,
  UTF-8/newline decoding, selected
  session/app history, cross-user 404, provider failure without partial history,
  explicit clear retaining the session, and terminal deletion. A controlled
  inference IO barrier also permits actual public clear/delete while the first
  model RPC waits: the session must already be visible, and the late completion
  must fail without restoring messages. A failed first model leaves a publicly
  clearable empty session. Only the actual
  signup-issued token and `/api/proxy` base mapping are injected into the client;
  its requests, responses and SSE parser are unchanged. This is a text-chat
  contract, not a browser UI, tools/attachments or full chat qualification.
- `share.mjs`, real Auth/API/D1 creation and consumption of task and selected-chat
  capability links. It proves the configured public Web origin, anonymous
  bounded previews, cross-user owner denial, self-accept denial, one successful
  recipient copy, duplicate acceptance, malformed input and invalid tokens.
  Python behavior tests cover expired and newly locked source records; the Web
  overlay tests cover expired/empty/malformed previews and every acceptance
  error state. Remote custom domains and Server OS browser execution remain
  separate evidence.

For an interactive isolated fixture:

```sh
node deploy/cloudflare/contracts/local-target.mjs \
  --output /tmp/new-owned-cf-target --brand-id local-fixture
```

For real local AI calls, put the following in a private mode-0600 file
outside the repository (for example `/secure/mimo.dev.vars`):

```dotenv
DEV_LLM_URL=https://token-plan-cn.xiaomimimo.com/v1/chat/completions
DEV_LLM_API_KEY=your-key
DEV_LLM_MODEL=mimo-v2.5
DEV_LLM_PROTOCOL=mimo
DEV_TTS_VOICE=mimo_default
DEV_OLLAMA_URL=http://127.0.0.1:11434/api/embed
DEV_EMBEDDING_MODEL=bge-m3
```

`DEV_LLM_PROTOCOL=openai` (the default) supports other OpenAI-compatible chat
endpoints, including loopback HTTP, for chat only. MiMo mode additionally selects
`mimo-v2.5-asr` and `mimo-v2.5-tts`; Ollama must already serve local BGE-M3.
The public embedding endpoint is projected to the BGE-M3 1024-dimensional
contract used by memory vectors (its production default BGE base model is
unchanged). No fixed embedding or transcript is returned in this live mode.
Endpoint/model selection is explicit; an
ambient environment variable never switches the deterministic CI provider.

```sh
# Interactive real LLM/ASR/TTS + Ollama through the same local Workers:
npm --prefix deploy/cloudflare run dev:product -- \
  --output /tmp/new-owned-cf-live --llm-dev-vars /secure/mimo.dev.vars

# The existing blocking product runner: common HTTP + real chat/audio/embedding.
# Default invocation without these arguments retains every deterministic suite.
npm --prefix deploy/cloudflare run test:product -- \
  --llm-dev-vars /secure/mimo.dev.vars
```

The local projection binds `AI` to `provider-dev.ts`; Python and TypeScript
business owners still call `env.AI.run()`. Production templates and prompts are
unchanged. Secrets are copied only to the disposable provider's private
`.dev.vars`; fixture evidence records endpoint/model/protocol without the key.
Keep that disposable output private as it contains local credentials.
`local-secrets.mjs` reads generated privacy/admin credentials with Node's dotenv
parser, including quoted values emitted by the local target. The recording and
chat contract runners share this reader; raw-line regular expressions rejected
those valid quoted credentials in the 2026-09-08 normal CI rehearsal. Missing or
malformed values still fail without printing the credential.

`dev-llm.mjs` preserves messages, usage and OpenAI choices, and returns Workers
AI's `response` field. MiMo uses `max_completion_tokens`, disabled thinking and
auto tool selection, matching the existing Server development adapter. For
`json_schema` requests the same schema becomes a function parameter schema;
the function arguments must be valid JSON and the unchanged application parser
owns full validation. No prompt is inserted, no failed request falls back to
fixed answers, and incomplete/unmetered results fail. See the
[MiMo protocol](https://mimo.mi.com/docs/en-US/api/chat/openai-api).

Live audio verification requires `ffmpeg` and `ffprobe` on the runner. The
product lane checks decoded desktop/mobile MP3 from the public TTS routes,
retranscribes generated speech through `/v1/stt/transcribe-workers-ai`, checks
Ollama vector size and semantic ordering through `/v1/embeddings`, and sends
real PCM through `/v4/listen` before rereading the persisted D1 transcript.

The real chat case set uses the upstream Web client against actual Auth,
Python `AI.run()` RPC, public SSE and D1 history; it also checks account
isolation, structured goal advice and clear. Model wording is not asserted.
These cases execute only when explicitly selecting a real provider; normal CI
remains hermetic, and adapter regressions run in the existing Vitest CI lane.
The runtime regression also executes the real `Provider` RPC in locked workerd
with an internal outbound Worker. It catches the observed local `redirect:
"error"` rejection; the adapter uses `manual` and rejects 3xx before any second
request. Network errors and response bodies never enter provider logs; only
fixed failure reasons and the three token counts are recorded.

This adapter supports non-streaming model chat. The current public chat path
already emits client SSE after non-streaming model inference; it does not claim
token streaming from MiMo. ASR/TTS requests use MiMo's documented
[audio ASR](https://mimo.mi.com/docs/en-US/api/audio/Speech-Recognition) and
[audio TTS](https://mimo.mi.com/docs/en-US/api/audio/tts) Chat Completions bodies.
TTS uses the explicitly selected MiMo voice and native MP3 output; it does not
reproduce Aura/ElevenLabs voice identities, sample-rate/bitrate tuning or Opus.
Ollama uses [`/api/embed`](https://docs.ollama.com/api/embed), `truncate:false`,
and rejects wrong model/dimension/count/non-finite/zero vectors.

The loopback ASR bridge is authenticated with a per-run secret and accepts mono
PCM16/linear16 or unsigned PCM8, 8–48 kHz, with `zh`, `en` or automatic language.
It serializes five-second recognition windows, flushes a short tail after
350 ms input inactivity, caps queued PCM at 20 seconds and cancels pending
inference on disconnect. Realtime owns transcript persistence. A client must
remain connected until its final transcript arrives; this is chunked MiMo HTTP
recognition, not MiMo-native WebSocket ASR, diarization or speaker identity.
Opus/stereo input is rejected explicitly rather than interpreted as PCM.
The transient Vectorize double remains local-only; passing this suite is not
production Cloudflare model/index or complete release qualification.

To exercise an already prepared release's exact application bytes, add
`--candidate /absolute/new-candidate`. The runner reopens the immutable candidate
and verifies its source and artifact hashes before copying all eight Workers,
Python dependencies, Web assets and Auth/App SQL into its private output. It
does not recompile those Workers. Their brand/persona/contact/firmware inputs
come from the candidate. Only resource names, local secrets, origins and provider
IO are projected; native account bootstrap policy is preserved exactly. Enabled
migration, hybrid or anonymous-registration policies
without a local contract are rejected instead of being disabled. Module, asset
and SQL bytes are rechecked after startup and after the selected product suites.

Frozen mode exposes Web on its own local port, using Wrangler's
[cross-command service bindings](https://developers.cloudflare.com/workers/local-development/multi-workers/)
to the owned Edge. `--run-share` also executes the actual frozen Web's anonymous
task/chat proxy, checks reduced payloads, private caching and missing-link errors.
It also fetches `/favicon.png` and `/logo.png` and compares their bytes to the
frozen assets. That check proves delivery integrity, not that the selected brand
has replaced those assets; visual/manifest identity is a separate assertion.
Web SSR and assets run unchanged. Browser API/auth URLs remain baked into the
production bundle, so local Web proof does not claim browser sign-in to deployed
services. `fixture.json` records `artifact_mode`, the candidate digest and this
boundary; all these local reports still set `release_qualified: false`.

The parent output directory must exist; the final directory must be new, including
no broken symlink. `metadata.json` contains `api_origin`, `auth_origin`, `target`,
`brand_id`, and `trace_dir`. `--run-core`, `--run-recording`, `--run-chat` and `--run-share` select the executable
suites and stop the target afterward. Otherwise SIGINT/SIGTERM stops it. Startup,
commands and teardown share one process-group owner: cancellation prevents later
stages, kills descendants, and never adopts an already-running endpoint. Each
command has a detached supervisor that stays alive until the tool's exit result
has reached the parent, which then kills the owned group. The supervisor forwards
the caller's standard streams and extra control descriptors unchanged; its IPC
uses a separate final descriptor. A cleanup denial is a failure carrying the
original tool result, not an uncaught callback or an accepted success. The recording signup driver honors one server-provided, bounded
`X-Retry-After` response when the shared loopback signup rate limit is reached;
that 429 remains in the trace and no auth rule is disabled. Each
command has a five-minute deadline, readiness two minutes, and an interactive
fixture one hour. Logs, synthetic secrets and report files stay in the new private
output directory. The shell prints this path; it retains evidence/state rather
than deleting unknown directories. No Cloudflare remote operations are issued.

`local-config.mjs` consumes current production Worker/binding owners and rejects
unknown owners or unsupported newly introduced runtime primitives. D1, R2, DO,
queues and service bindings are local and uniquely named. It clears remote
credentials from child environments and replaces deployment origins/secrets with
loopback/per-run values. It never consumes a release inventory or approval.
The local fixture explicitly supplies synthetic public brand metadata
(`Local Atlas` / persona `Mira`) bound to its selected brand ID and its explicit
`support@atlas.example.invalid` public contact. It cannot inherit
the upstream brand/contact from Worker templates, and the projection rejects missing or
cross-brand input. This synthetic fixture is not a production brand manifest;
resource-plan tests separately execute the real manifest/profile renderer.
Source mode enables native ownership bootstrap on Core/Edge/Realtime, matching
the production renderer's new-allocation policy; migration mode stays disabled.
The presentation case temporarily sets warning/restrict through the actual local
fair-use admin endpoint using this fixture's private generated key, reads the
public user status, then resets in `finally`. Traces contain route/status only;
no admin credential leaves the loopback target or appears in the report.
`fixture.json` records that public brand/contact metadata, source revision/status,
npm/Python locks, tool versions,
normal migration files, exact frozen module hashes and configuration hashes.
`cache-owner.json` is written before startup, including on a failed run. By
default the fixture creates its own private `pyodide/` directory before workerd
starts; an explicit `CLOUDFLARE_PYODIDE_CACHE_DIR` must already be an ordinary
directory and is recorded as external reuse. The runner never creates or deletes
an explicit external cache. Workerd still verifies cached package integrity.

The ASR WebSocket and structured/text inference provider are controlled; application
routes, Auth sessions/JWT, data ownership, D1 storage and Queue delivery are real.
Wrangler's Node test harness cannot transport an outbound WebSocket upgrade, so
the runner uses its actual CLI with the fixed native-workerd launcher. Transcript
quality, hosted model behavior, remote Vectorize/Images, custom domains, previous
schema compatibility, every product route and client platform remain unproved.
Fresh Python package/cache downloads require network and valid TLS; a verified
local Pyodide cache may be reused via `CLOUDFLARE_PYODIDE_CACHE_DIR`. A cache failure
fails the runner; it never starts a Core stub or returns a substitute success.

The 2026-09-04 integrated Node 22 run exposed both the absent default cache and
an exited-group cleanup race: workerd rejected the missing directory, then
Wrangler's close callback replaced that failure with `kill EPERM`. The same
frozen command reproduced that cleanup error in four of five immediate reaps;
holding the live supervisor preserves the actual tool status. The SQLite import
dry-run test also compares stderr with a clean in-memory `node:sqlite` process
on the same Node executable: only its exact experimental warning is accepted,
and additional application diagnostics remain failures.

These product reports always set `release_qualified: false`. They do not replace
CF5's pending complete-product/dual-target qualifiers or its schema acceptance.
`qualify-prior-schema.mjs` is the separate fixed first-release schema runner. It
consumes the validated frozen candidate directory plus fresh remote observations,
executes frozen SQL/legacy fixtures, and verifies actual empty/deployed D1 catalogs.
It refuses existing prior Workers and restore phases until an executable retained
version compatibility harness exists. Its HTTP/process boundary and SQL behavior
tests run in the existing full Cloudflare Vitest local/CI lane; a controlled API
test is not a remote account proof.
The recording lane also exercises referral link capture, four concurrent
claims, the thirty-day Operator subscription projection, owned export and
the existing queue-driven account erasure. The signup Web UI is verified
separately against the same local HTTP authority; this lane alone does not
prove a browser flow or production deployment.

## Current local execution and candidate regression

From `deploy/cloudflare`, `npm run test:product` runs the existing actual
Wrangler product lane. `npm run dev:product -- --output /absolute/new-target`
keeps the isolated API/Auth Workers running for interactive HTTP work; startup
prints `metadata.json` with the actual loopback origins. Add `--candidate` to
use frozen brand/application/Web bytes as described above. Source-mode fixtures
use the explicitly synthetic Local Atlas presentation.

The 2026-09-08 source run passed 49 cases (16 common, 20 recording/privacy,
11 chat, two share). The separate standard Docker Server run passed all 16
common cases. `contracts/deployment/regress.mjs` now composes those existing
runners for a frozen candidate and checks matching common case identities.
`release.mjs prepare` executes it as a blocking local release step. These runs
exercise actual persistence/Queue owners with controlled model IO; live model
quality, remote bindings and production readiness require their own evidence.
