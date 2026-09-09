# Fork CI and two independent deployment workflows

Both backends use `summersmile1984/omi`, one `main`, and one verified source
commit. Cloudflare and Server OS have separate deployment workflows, state,
domains, environment credentials and serialization locks.

## Entry points

| Workflow | Input | Effect |
| --- | --- | --- |
| Fork Checks (`fork-checks.yml`) | Manual run at the selected commit | Runs the complete portable and native check manifest. Push/PR runs remain diff-scoped. |
| Fork Release Preparation (`fork-release-prepare.yml`) | Successful full `ci_run_id`, `brand`, `stage`, optional `inventory_json` | Revalidates the exact CI SHA and freezes both delivery archives. No deployment credentials. |
| Fork CD Cloudflare (`fork-cd-cloudflare.yml`) | Successful `delivery_run_id`, `stage` | Checks main ancestry and artifact hashes, then publishes the frozen nine-Worker candidate with qualified migrations and remote observations. |
| Fork CD Server OS (`fork-cd-server.yml`) | Successful `delivery_run_id`, `stage` | Imports accepted Docker image IDs, boots them against disposable state, snapshots an existing deployment, migrates and starts the persistent service, then checks public HTTP contracts. |

Deploy workflows must be dispatched from `main`. The artifact's source must
already be integrated into `main`; a feature branch cannot use production
secrets. Each target/stage has its own concurrency group with cancellation
disabled. A successful prepare run can feed either deployment independently.
Preparing the same SHA does not rerun the entire Fork Checks workflow. The
existing Cloudflare build owner still runs its local component qualification
and Wrangler/Docker business regression before freezing artifacts.

```sh
# Select the exact commit with a successful manual full CI run.
gh workflow run fork-release-prepare.yml --ref main \
  -f ci_run_id=CI_RUN_ID -f brand=eddy -f stage=beta

# Use the resulting successful preparation run ID in either independent CD.
gh workflow run fork-cd-cloudflare.yml --ref main \
  -f delivery_run_id=PREPARATION_RUN_ID -f stage=beta
gh workflow run fork-cd-server.yml --ref main \
  -f delivery_run_id=PREPARATION_RUN_ID -f stage=beta
```

Preparation requires a clean committed checkout, pinned component dependencies
and Docker. `prepare_release.py --plan` prints its build recipe without
execution. The supported Server image platform remains `linux/amd64`.

Cloudflare CD creates the Python 3.14 interpreter consumed by its frozen HTTP
contract runner before downloading artifacts. The HTTP client uses only the
standard library; the frozen Python Workers already carry their runtime
dependencies. A fresh checkout removes the API project's ignored `.venv`, so
CD must not rely on the environment produced as a side effect of preparation's
pytest run. This target-specific setup is inline in the deployment workflow:
an already accepted artifact keeps its original source and bytes when the
workflow's tool provisioning is repaired. `test_release_ci.py` executes this
step from a clean fixture and verifies provisioning failure stops the probe.

## Artifacts and provenance

Both CD workflows obtain the selected artifact's current metadata from GitHub,
then download its ZIP with bounded, resumable curl requests. Each request stops
after five minutes or thirty seconds without meaningful progress; at most five
requests run per invocation. Signed URLs are sent through curl's stdin rather
than command arguments or logs. Partial ZIP bytes survive interruption under
`~/.cache/eddy-delivery/summersmile1984-omi/<artifact-id>/<zip-sha256>/`.

A cache hit must match both the size and SHA-256 freshly returned by GitHub.
Only then are the four ordinary delivery files extracted into an isolated
folder and handed to admission. Corrupt or incomplete bytes never become a
verified cache or deployment input. The subsequent source, stage and archive
hash checks remain mandatory. This handles the observed v4 silent truncation
in run 34362300799 and the stalled transfer in run 34367542966 without dropping
already received bytes or changing accepted application payloads.

The Cloudflare archive transports `candidate.json` plus only the files declared
by that candidate's `artifact_files` owner. Staged dependency links and build
scratch directories are excluded. Admission recomputes the candidate digest,
checks every declared file hash, rejects links/duplicates/missing payload files,
and never materializes unlisted archive members. The existing Node candidate
owner still verifies source identity and complete frozen trees before publishing.
The full archive hash in `delivery.json` is checked before selective extraction.

Each CD loads the independent download/admission utilities and archive module from
`GITHUB_WORKFLOW_SHA` using the fetched Git objects into `RUNNER_TEMP`. The
application checkout stays at the admitted source SHA. This lets a workflow
repair its transport/decoding without changing accepted application bytes or
injecting untracked controller sources into the candidate identity.

Artifacts are retained by Actions for 30 days. `delivery.json` binds source
commit/tree, full CI run/attempt, brand/stage, Cloudflare candidate digest,
Docker image IDs and each archive's SHA-256. `cloudflare.tar.gz` holds the
original frozen Worker/Web/SQL candidate; `server-images.tar` holds backend,
Auth and Web images, plus the LLM runtime only for native inference profiles;
`source.tar.gz` holds committed source. Preparation and deployment derive the
required image roles from the same frozen brand/stage profile. Missing or extra
roles fail admission.
Model weights and runtime credentials are outside the archives.

The CD resolver verifies the fork, workflow path, successful manual run, main
ancestry, stage-specific artifact and full CI jobs. The consumer verifies the
archive bytes again. Server image inspection additionally verifies Linux
architecture and commit/tree labels. Neither deployment rebuilds accepted
application images. Local build receipts alone never authorize deployment.

## GitHub configuration

| Environment | Secrets | Variables |
| --- | --- | --- |
| `cloudflare-beta`, `cloudflare-production` | `CLOUDFLARE_API_TOKEN`, `RELEASE_SECRETS_JSON` | `RELEASE_JOURNAL_ROOT` |
| `server-beta`, `server-production` | Runtime secrets remain on the selected deployment host | `SERVER_DEPLOY_ROOT`, `SERVER_DOCKER_CONTEXT` |

All four environments allow only the `main` branch. Repository variables
`CF_INVENTORY_BETA` and `CF_INVENTORY_PRODUCTION` supply the preparation defaults.
Inventories contain resource identities and secret reference names, never
secret values. The Cloudflare secret bundle maps those names to actual values;
the deploy wrapper resolves only references present in the accepted candidate.
The token is scoped to the selected account and `smartipproxy.com` zone.
Cloudflare retains a copy of each accepted candidate beside its transaction
journal, so Actions temporary-directory cleanup cannot remove recovery inputs.
The name-only classifications live in
`config/deployment-setting-classification.fork.json`. The fork manifest runs
the existing upstream secret-boundary checker with the additive policy;
reclassification and fork exceptions are refused. The standalone upstream
checker only loads its own policy and consequently cannot classify fork CD
settings. This limitation is tracked with the upstream preflight incompatibilities
in `dev/unified-main/implementation-2026-09-05/fork-first-push-2026-09-09.md`.

## Mac Studio Server owner

The host uses the dedicated Colima profile/context `eddy-server` /
`colima-eddy-server`. CI continues to use Docker Desktop. CD rejects default
and `desktop-linux` deployment contexts. Each stage stores its runtime file,
backup key, retained Git checkouts, image receipts, encrypted snapshots,
acceptance traces and transaction journals outside Actions `_work`:

```
~/.local/share/eddy-server/{beta,production}/
  runtime.env      # mode 0600, reviewed Compose environment
  backup.key       # mode 0600, 32 random bytes, outside snapshot directories
  gateway.json     # pinned nginx digest and stage-specific loopback port
  current.json     # advanced only after public acceptance succeeds
  releases/<sha>/  # independent retained Git checkout and deployment journal
```

`operations.sh deploy-images` uses the existing migration/health owner while
requiring the accepted image receipt. `start` retains its local build behavior.
The provider startup sequence starts the selected model services before callers.
The shared brand profile selects the actual model-service set: Eddy beta and
production use MiMo chat/ASR/TTS plus local BGE-M3/Ollama Embedding, requiring
`MIMO_API_KEY` in the private host environment. Native profiles retain both
local model services. No runtime secret can switch the frozen provider choice.
A unique `eddy-boot-*` project exercises the delivered images before persistent
state is changed and removes only its own disposable volumes afterwards.

The gateway runs the accepted Web image and pinned nginx, preserving SSE and
WebSocket upgrade traffic. It binds only a loopback port. A separately managed
Cloudflare Tunnel maps four Server hostnames to that gateway. `/internal/`
routes and unmatched hosts return 404. MinIO signed requests preserve their
original Host header. Cloudflare Workers use separate hostnames and resources.

Server beta origins are `eddy-server-beta-api`, `eddy-server-beta-auth`,
`eddy-server-beta`, and `eddy-server-beta-objects` under `smartipproxy.com`.
Cloudflare beta uses `eddy-cf-beta-api`, `eddy-cf-beta-auth`, and `eddy-cf-beta`.
Production drops `-beta` from these prefixes. The brand manifest owns every
origin; environment files cannot silently replace it.

## Qualification and recovery boundaries

Cloudflare's product runner executes the existing local Wrangler business
suites before publishing. After publishing it checks live Auth, Web streamed
chat, API smoke contracts, private R2 object isolation, MCP key revocation,
authenticated native/Web recording sessions, and native TTS-to-ASR. The
separate dual-target runner binds full platform CI and compares the same
executed core business cases on Server Docker and hosted Cloudflare. Hosted
checks create their own accounts and delete only those accounts; cleanup
failure fails qualification. These are explicit API/provider checks, not a
claim that every native UI journey was exercised against the public endpoint.

The existing prior-schema qualifier currently admits first release into empty
D1 only. An update with retained Worker versions, arbitrary schema migration,
or rollback must provide the separate prior-version/new-schema compatibility
proof; these entry points fail closed until that proof exists. D1 migrations
are never automatically reversed. `release.mjs recovery-plan` reports the
observed candidate/journal recovery state.

Server failures retain the previous pointer, accepted images, encrypted backup
and failure journal for reconciliation. Automatic destructive data restore is
not part of the deployment workflow. Use the existing `operations.sh`
backup/verify-backup/rollback-plan/restore owner with the matching retained
source, environment and encryption key. See `deploy/self-host/README.md`
and `deploy/cloudflare/release.md` for their recovery contracts.
Before advancing the Server pointer, CD also checks public chat streaming and
persisted history, a synthetic TTS-to-ASR round trip, and real Embedding
inference through the admitted driver inside the accepted backend container.
The historical Server realtime multimodal relay is not implemented; its unused
environment variables no longer block startup.

## Verification

Hermetic provenance, image admission, private journal writes and hosted-account
cleanup tests run in the existing fork manifest lane. Workflow syntax uses
`actionlint`. Cloudflare product-runner tests run in its Vitest suite. Live
provider/public-origin tests run during CD and are not hermetic CI tests.
Successful CI, successful preparation, and successful public deployment are
separate results; only a completed deployment journal proves the last one.
