# Beta CD preparation evidence — 2026-09-09

The independent Cloudflare and Server CD workflows consume a successful full
Fork Checks SHA and frozen archives. GitHub environments and credentials are
configured; the Cloudflare beta storage resources and Server Tunnel exist.
This record does not claim a completed public deployment.

Server source `2be37887f9` was built and booted as the isolated Docker Desktop
project `eddy-preview-2be37887`, using the beta profile and production Auth
guards. MiMo owns chat/ASR/TTS; local Ollama owns BGE-M3 Embedding. No default
prompt was changed. The twelve containers, including the completed artifact
check, reached the expected states.

Observed commands and results:

- `contracts/deployment/core.py --metadata <private preview metadata> --remote`:
  all 16 business cases and two owned-account cleanups passed after separating
  the loopback transport origin from the public referral issuer.
- `scripts/fork/server_ai_acceptance.py --metadata <private preview metadata>`:
  authenticated streaming chat and persisted history passed; real TTS WAV to
  desktop PCM ASR passed and recognized the synthetic work-notes phrase.
  The upload-ASR case remains failed in this isolated preview because the
  public object hostname has no serving gateway yet. The request reached that
  hostname and returned 502 before ASR. Public CD still requires this case.
- The accepted backend's actual Embedding driver returned 1024 dimensions
  from the locally provisioned and hash-verified BGE-M3 artifacts.
- Focused release preparation, installed-image, hosted-core and Compose-model
  tests cover native and MiMo image roles, rejected missing/extra images,
  unchanged source identity and public-origin retention. The shared fork CI
  script lane executes these tests.

Corrections from live acceptance use the existing wire contracts: chat's
`utils.chat_followup.split_followup_tail` trims whitespace before a follow-up
chip; the multipart ASR endpoint requires `files` and accepts WAV, WebM, MP4
or sync BIN input. The fixture uses WAV. It continues to reject changed or
missing streamed answer content. No inference output is rewritten.

Preparation now derives image roles from the same selected profile as Compose:
MiMo requires backend/Auth/Web images; native profiles additionally require
the local LLM runtime. Building the old unconditional LLM image would read a
missing LLM configuration from the MiMo backend image.

The main-relative upstream preflight exceptions remain tracked under
`CI-FORK-2026-09-09-1` in `fork-first-push-2026-09-09.md`. The bounded push gate
uses the previously published feature head. Fork actionlint and additive
settings classification run normally; the incompatible upstream equivalents
use their existing explicit hatches. Full CI and public CD are not bypassed.

## Public infrastructure verification

Both named Server Tunnels route their separate beta/production hostnames to
the dedicated Colima gateway ports. Production runtime configuration passes
`check-config.py`; state/encryption/auth keys are independent of beta.
An owned MinIO + production gateway probe over the public beta Tunnel returned
200 for valid SigV4 URLs, including a leading-slash object key, and 403 for an
unsigned request. The initial edge 1010 was Browser Integrity Check; an active
configuration rule disables only that check on the ten exact Eddy API/Auth/
object hostnames. Web hosts and backend authentication/signatures retain their
configured policies. Full upload-ASR remains a CD acceptance gate.

## CI runtime failures reproduced and repaired

Linux PR run `34331397206` returned 500 instead of 404 for an invalid referral
code after an unauthenticated POST. The same frozen source reproduced the
failure locally: Wrangler 4.127.0's ProxyWorker reported `Network connection
lost` and terminated. This matches workers-sdk issues #14641/#15317; request
headers or buffering did not repair it. The test target now uses the pinned
Wrangler binding converter and direct Miniflare/workerd HTTP sockets, including
a Web socket in the same runtime so EDGE bindings share state. All 16 common
cases and the complete recording/chat/share/privacy suite passed through that
runtime. Component Vitest: 135 files, 1114 tests passed.

Independent Mac run `34331389577` failed memory batch intake with 503. A repeated
public memory lifecycle reproduced `MemoryOperation` validation:
`created_at=2026-09-09T09:37:40.328999Z`, followed by an update clock read of
`09:37:40.328000Z` (999 microseconds earlier). This occurs before D1 batch
submission and is not a transaction or CAS-admission failure. The fork clock
seam runs the original transition body with time floored at the prior revision;
strict decoding, terminal-state rules, identity and generation guards remain.
The same seam is installed by Server startup/maintenance and CF projection.
No upstream source or test file is edited. Controlled clock tests cover
regression below the prior update and below creation, and real HTTP/D1 intake.

Final fork-clock validation: `uvx uv==0.12.3 run pytest -q` in API Core passed
1296 tests; the complete fork startup manifest passed 291 assertions; the final
real workerd product run passed 49 cases. The main-relative upstream PR
preflight still stops at the already tracked line-count ratchet for upstream
synchronization growth; the fork acceptance lanes remain enforced.

Ubuntu PR run `34337809124` subsequently passed the complete Cloudflare product
suite, then stopped while starting the Server dependency containers. A native
Linux reproduction established that the generated profile inherited mode `0600`
and the non-root provider failed its startup import with `PermissionError`.
The public profile is now explicitly read-only (`0444`); the fixture directory
and credentials retain their private permissions. The existing product lane
recreates the actual profile bytes/mode on a Linux filesystem and imports the
real provider after dropping to its container UID, while also proving private
file denial. That check rejects the original mode. The changed Server product
lane passed all 11 fixture tests, the native permission proof and all 16 HTTP
cases locally. A new remote CI run is still required for this source change.

At `72f78ee226`, Ubuntu run `34339851267` passed both real backend product
suites and the Electron/Flutter fork lanes. Two later Vitest cases hit the
unchanged five-second test deadline under suite load. OAuth bundled three
independent migrated databases under one deadline; those admission cases now
run separately with every assertion retained. Hume's near-limit fixture
re-serialized the entire growing array on each iteration; incremental length
accounting builds the same payload in linear time. Runtime and test timeouts
remain unchanged. The corresponding local Linux measurement used two CPUs.

The Mac runner also encountered GitHub's cancellation redelivery pattern
(actions/runner #4569), recovered after force-cancelling the superseded push
and restarting its service. Its inherited Docker Desktop credential helper
later hung during public-image metadata resolution. A separate runner Docker
configuration using the native Keychain helper pulled the exact pinned digest
successfully and retained both Docker contexts; the user's Docker configuration
was not edited. The runner service loads that configuration on its next job.
