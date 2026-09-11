# Fork CI and two independent deployment workflows

Both backends use `summersmile1984/omi`, one `main`, and one verified source
commit. Cloudflare and Server OS have separate deployment workflows, state,
domains, environment credentials and serialization locks.

## Entry points

| Workflow | Input | Effect |
| --- | --- | --- |
| Fork Checks (`fork-checks.yml`) | Manual run at the selected commit | Runs the complete portable and native check manifest. Push/PR runs remain diff-scoped. |
| Fork Release CI (`fork-release-prepare.yml`) | Successful full `ci_run_id`, `stage`, optional `inventory_json` and `continue_from` | Freezes once, downloads the actual GitHub artifact through the CD transport, qualifies all nine Workers in Cloudflare and boots the accepted Server images with disposable state. |
| Fork CD Cloudflare (`fork-cd-cloudflare.yml`) | Successful `delivery_run_id`, `stage` | Checks main ancestry and artifact hashes, then publishes the frozen nine-Worker candidate with qualified migrations and remote observations. |
| Fork CD Server OS (`fork-cd-server.yml`) | Successful `delivery_run_id`, `stage` | Imports accepted Docker image IDs, boots them against disposable state, snapshots an existing deployment, migrates and starts the persistent service, then checks public HTTP contracts. |

Deploy workflows must be dispatched from `main`. The artifact's source must
already be integrated into `main`; a feature branch cannot use production
secrets. Each target/stage has its own concurrency group with cancellation
disabled. A successful Release CI run can feed either deployment independently.
Preparing the same SHA does not rerun the entire Fork Checks workflow. The
existing Cloudflare build owner still runs its local component qualification
and Wrangler/Docker business regression before freezing artifacts.

```sh
# Select the exact commit with a successful manual full CI run.
# The deploy brand is not an input: it is declared in
# config/repo-state.fork.json and checked by `fork-repo-state`.
gh workflow run fork-release-prepare.yml --ref main \
  -f ci_run_id=CI_RUN_ID -f stage=beta

# Use the successful Release CI run ID in either independent CD.
gh workflow run fork-cd-cloudflare.yml --ref main \
  -f delivery_run_id=RELEASE_CI_RUN_ID -f stage=beta
gh workflow run fork-cd-server.yml --ref main \
  -f delivery_run_id=RELEASE_CI_RUN_ID -f stage=beta
```

Preparation requires a clean committed checkout, pinned component dependencies
and Docker. `prepare_release.py --plan` prints its build recipe without
execution. The supported Server image platform remains `linux/amd64`.

Source checks alone never authorize CD. Release CI must finish all four stages:
`Freeze delivery artifacts`, `Cloudflare artifact qualification`, `Server image
qualification`, and `Release ready (runtime and public ingress)`. Each target stage calls its existing CD workflow in
qualification mode, yielding six executable jobs in total. Both CD resolvers
require those exact job names and successful outcomes in the selected run attempt. Historical build-only
preparation runs, the older `Release ready` contract (including green run
34415705069), skipped qualification and failed qualification are rejected.
An older green run must requalify through the current workflow; it cannot supply
the newly required public readiness and ingress evidence.

The job names alone cannot prove the run executed the *complete* manifest: the
same two jobs also serve the diff-scoped push and pull-request lanes, so a
dispatch that quietly selected less would look identical. The complete lane
therefore writes a manifest attestation per job (`fork-ci-attestation.json`,
published as `fork-ci-attestation-linux-<sha>` and
`fork-ci-attestation-macos-<sha>`), recording the manifest digest, the run and
attempt, the source commit and the check ids that job selected. Admission
requires the union of those ids to account for every `ci`-lane check in the
manifest at the delivered commit, and rejects a missing artifact, a different
manifest digest, another run attempt, or a foreign lane. A run whose checks
failed publishes nothing, because the attestation step only runs after every
check passed.

The freeze job has no deployment credentials. The reusable target workflows own
both qualification and deployment: the same tool setup, artifact transport,
verification, environment credentials and concurrency lock run in both modes.
Only `workflow_call` accepts `qualification_artifact`; the ordinary manual CD
inputs cannot bypass completed Release CI admission. Qualification accepts only
the calling run's exact SHA/stage artifact and returns before persistent promotion.
Both modes use the existing target/stage GitHub environments, restricted to `main`.
[GitHub's reusable workflow contract](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)
keeps same-repository calls at the caller's commit and uses the environment
secrets declared by the called job. The Cloudflare caller must also declare
`secrets: inherit`: without it, run
[34401734852](https://github.com/summersmile1984/omi/actions/runs/34401734852)
selected the correct environment but received empty credentials. Both Cloudflare
modes now reject missing credentials before tool setup or artifact download.
The workflow contract test checks this call wiring and executes that early
failure path without exposing values. Deployment settings remain owned by the
existing target workflows, so the upstream name-only diff checker and the
fork's additive classifier both run without a new classification exception.

Cloudflare qualification reuses the actual frozen publisher with all uploaded
module/assets bytes, variables, secrets and compatibility settings. It creates
one complete private directory per Worker before changing its configuration:
Wrangler 4.127.0 collects `python_modules` relative to the configuration project
root, even when `main` and `base_dir` are absolute. Moving only the configuration
silently drops vendored dependencies; the live trial of artifact 10124622514
failed with error 10021 (`ModuleNotFoundError: fastapi`) for this reason.
The regression runs the pinned Wrangler dry-run and checks the emitted Python
package bytes. Earlier negative trials that moved only the configuration do not
prove that the original Core error 10013 was reproduced.
The qualifier creates
two uniquely owned temporary D1 databases, executes the frozen SQL through the
real remote migration command and compares catalogs, ledgers and foreign keys
with the existing frozen SQL owner. Trial Workers bind to those temporary D1
IDs; other resource bindings remain the candidate's.
Nine temporary private Workers use isolated Worker/DO identities and service
bindings within that temporary set. Public routes, Cron and Queue consumers are
not installed. A tenth, token-guarded gateway allows only the six fixed read-only
health/readiness/login paths, and checks each twice for application initialization and a
subsequent request. Auth's signing-key bootstrap stays in temporary D1; Edge
readiness also exercises the trial service graph and private rate-limit DO.
The production schema/continuation owner also checks the real
D1 catalogs and active version basis before upload. Every probe is observed and
deleted only with matching transaction/version annotations; temporary D1 deletion
requires the independently returned and observed creation ID. An unknown external
version is retained for reconciliation and fails CI. Evidence lives in a private
`ci-<commit>-<uuid>` directory under `RELEASE_JOURNAL_ROOT`.

Both target readiness contracts come from `contracts/deployment/readiness.json`.
Cloudflare checks the same Web-to-Edge `/api/worker-ready` JSON through the frozen
local build, private cloud gateway and public CD; login SSR is checked separately.
Server image boot and public CD reject HTML/degraded JSON from API/Auth `/ready`,
while Web `/login` remains an SSR check. Before cloud upload, the existing release
adapter also checks both frozen target profiles' public ingress using Cloudflare
Request Trace (`skip_response=true`). The existing Cloudflare token needs account
**Allow Request Tracer: Read** in addition to its existing permissions. These
read-only checks catch BIC/WAF route mismatches even for first publication, without
creating public trial routes. Public hostnames must already have DNS records:
the API rejects an unregistered host even with `skip_response=true` (observed for
`eddy-cf-api.smartipproxy.com` on 2026-09-10). Missing DNS fails Release CI rather
than being accepted as an empty policy. They do not replace public business acceptance.

This cloud rehearsal catches actual upload/startup failures such as beta run
34373519437; a local Wrangler dry-run cannot substitute for it. It does not
promote temporary Worker version IDs to the target names. CD publishes the same
verified frozen bytes and rechecks live state and public business behavior.
Durable Object Workers do not support ordinary Preview URLs, and data-resource
state is outside Worker version identity; see the official [Preview URL limits](https://developers.cloudflare.com/workers/versions-and-deployments/preview-urls/#limitations)
and [version model](https://developers.cloudflare.com/workers/versions-and-deployments/).

Server qualification invokes the same `deploy_server.py` image loader, identity
validation and `boot_test` used by CD, with a separate execution directory and
disposable Compose project/volumes. A failed boot fails CI. A successful boot
records `artifact_qualified=true` and `release_ready=false`; it does not back up,
restart or promote the persistent service. The exact downloaded images are
exercised before the delivery run becomes eligible for either CD.
Qualification source and private evidence are retained under
`SERVER_DEPLOY_ROOT/qualifications/<commit>-<uuid>`, on the same host mount as
the real deployment. Do not stage bind-mounted source under macOS
`/var/folders`: that path is outside the default Colima host mounts and Docker
can create an empty directory where a settings file was expected.

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
commit/tree, full CI run/attempt, brand/stage, Cloudflare candidate digest and
the qualified `cloudflare_continue_from` journal name,
Docker image IDs and each archive's SHA-256. `cloudflare.tar.gz` holds the
original frozen Worker/Web/SQL candidate; `server-images.tar` holds backend,
Auth and Web images, plus the LLM runtime only for native inference profiles;
`source.tar.gz` holds committed source. Preparation and deployment derive the
required image roles from the same frozen brand/stage profile. Missing or extra
roles fail admission.
Model weights and runtime credentials are outside the archives.

The CD resolver verifies the fork, workflow path, successful manual run, main
ancestry, stage-specific artifact, all six executable Release CI jobs and full source CI.
The consumer verifies the
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

### Repository state policy

`config/repo-state.fork.json` is the single declaration of the repository
settings this pipeline depends on: the deploy brand, which workflows stay
enabled and why, which of their jobs gate `main`, which known-red checks are
quarantined, and the branch/reviewer protection of the four environments.

`scripts/fork/check_repo_state.py` runs in every lane. Offline it requires the
declaration to agree with the tree: every workflow file classified exactly once,
every required job a literal job name inside its own workflow, every quarantine
entry tracked and excluded from `main`'s checks, and every deploy path naming the
declared brand. In GitHub Actions it additionally compares the registered
workflows and their enabled/disabled state with the declaration, and fails closed
when that API is unavailable.

The ruleset and environment halves cannot be read with a `GITHUB_TOKEN`, so
they are not claimed as CI-enforced:

```sh
# Print the changes the declaration implies; nothing is written.
python3 scripts/fork/apply_repo_state.py

# Apply them. Refuses to arm the ruleset while a required check is failing on
# the latest run of its workflow on main -- arming it first would block every
# merge on a check that was already red.
python3 scripts/fork/apply_repo_state.py --apply

# Read the repository back and report every difference.
python3 scripts/fork/apply_repo_state.py --verify
```

The ruleset (`fork-main-gate`) requires the declared checks, requires changes to
reach `main` through a pull request, forbids deletion and force-push, and grants
the repository-admin role an `always` bypass as the break-glass hatch. The two
production environments carry a required reviewer; the beta environments do not.

A required check must be **reportable on every pull request**, so
`check_repo_state.py` refuses a required job whose workflow has no
`pull_request` trigger or filters it by `paths`. Neither case ever reports, and
GitHub then holds every pull request at "Expected -- waiting for status to be
reported" forever, which is strictly worse than not requiring the check. Jobs a
workflow skips internally are fine: GitHub records a skipped check and treats it
as satisfied. `openapi-contract.yml` and `release-eligibility.yml` stay enabled
but advisory for this reason -- the first is path-scoped, the second only runs on
push.

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

The existing prior-schema qualifier admits first release into empty D1, or
continuation of an owned incomplete first release with exactly matching SQL and
infrastructure. Release CI's optional `continue_from` input names the retained
journal (a basename under the stage's `RELEASE_JOURNAL_ROOT`). The owner checks
that journal and its retained candidate, follows earlier failed attempts back
to observed Worker absence, and locks the entire journal lineage. Every live
version must match the recorded owner; an ambiguous upload may only be retried
when the active version is provably unchanged. Already published Worker
upload payloads and configuration must be byte-identical in the freshly qualified
candidate. Wrangler's timestamped README and root source map are not uploaded
under the existing no-source-map contract; their fresh build metadata does not
change the retained code identity. The schema
runner observes full D1 catalogs, migration ledgers and foreign keys before any
continuation mutation. Publication gives all nine Workers the new candidate
annotations while retaining Worker identities and persistent data. The normal
source/CI/local-product and deployed-product gates still run. No old proof
approves new bytes, no prior journal is rewritten, and a completed release
cannot be adopted as an incomplete first release.
The continuation intent is part of the immutable `delivery.json`. Cloudflare
CD consumes that qualified intent directly; operators cannot substitute a
different journal in the CD dispatch inputs.

### Resetting a stage stuck between the two contracts

A stage can end up in a state neither contract accepts: an interrupted first
release has published some Workers and run the migration authorities, and the
next candidate changes one of those already-published Workers. Continuation is
refused ("continuation must preserve every already published artifact") and a
fresh first release is refused (Workers present, D1 authorities non-empty). The
release owner is explicit that this is out of scope -- "this narrow operation does
not implement arbitrary upgrade or rollback compatibility" -- and automatic
rollback is refused too, because a first release has no previous version to
restore and recovery may not delete unowned Worker, queue, domain or Durable
Object state.

The escape hatch is an operator reset. It is a pair of `reset_stage` /
`reset_confirm` dispatch inputs on the Cloudflare CD workflow, and it is NOT part
of any release lane: it runs `scripts/fork/reset_cloudflare_stage.py` inside the
stage's own `cloudflare-<stage>` environment, under the same per-stage
concurrency group as a release, prints the full plan, and requires the exact
`reset:<brand>:<stage>` confirmation token. It detaches the stage's Workers from
every queue they consume, deletes them, restores the stage's public hostnames,
and drops every trigger, view, index and table in each D1 authority (never
`sqlite_*` or `_cf_KV`) in dependency order, then re-reads each authority to prove
it is empty. The next release is then an ordinary first release with no
`continue_from`.

Deleting a Worker also deletes the custom domain that published it, and that
domain held the stage's DNS record: `qualifyPublicIngress` fails closed on a
hostname the API cannot resolve ("the API rejects an unregistered host even with
`skip_response=true`"). The record cannot be put back through the Workers
custom-domain API, because attaching one needs the Worker that will serve it
("This Worker does not exist on your account"), so the reset creates Cloudflare's
documented originless placeholder instead -- a proxied `A 192.0.2.0`. The release
then takes that reservation down immediately before it publishes and lets the
attach create its own record; the operation is described in
`deploy/cloudflare/release.md` and journaled as `adopt:ingress-placeholders`.
Cloudflare refuses the takeover outright (`100117 "already has externally managed
DNS records"`) and ignores the `override_existing_dns_record` option the pinned
Wrangler sends, so an externally managed record must be deleted rather than
replaced. Hostnames that already have a record of another shape are left exactly
as they are, and the attach then fails closed.

It uses the Cloudflare REST API directly, the way `release-wrangler.mjs` does.
`wrangler delete` first lists the account's KV namespaces, so a token scoped for
deployment fails every deletion with `Authentication error [code: 10000]`; the
queue-consumer detach is needed because Cloudflare refuses to delete a Worker
that still consumes a queue (code 10064), and `release.md` leaves that cleanup to
separate ownership -- this reset is that owner.

It lives in the CD workflow rather than one of its own for two enforced reasons:
a new workflow binding a fork-classified deployment secret is an unclassified
binding that the upstream secret-boundary check refuses, and the release
admission requires this reusable workflow's job names to be exactly the release
job set -- so the reset is steps gated by `if: ${{ inputs.reset_stage }}` inside
the existing `deploy` job, never a job of its own.

The reset deletes remote state and exists only because the two contracts do not
meet. Delete the reset inputs, their steps and
`scripts/fork/reset_cloudflare_stage.py` once the stage is released, and record
the episode against the release owner that should have handled it.

An update with changed retained Worker artifacts, arbitrary schema migration,
or rollback must provide the separate prior-version/new-schema compatibility
proof; these entry points fail closed until that proof exists. D1 migrations
are never automatically reversed. `release.mjs recovery-plan` reports the
observed candidate/journal recovery state.

### Failure evidence contract

A failed release must explain itself from the run page; reconstructing the cause
by hand on the host is not an accepted recovery path. Both CD workflows run
`scripts/fork/release_failure_summary.py` as an `if: failure()` step: it finds the
journals that belong to the exact admitted source under the target's journal root,
copies them into a `fork-release-evidence` artifact, and writes a short markdown
block into the job summary. It exits 0 in every case -- including a missing or
malformed journal -- because the step must never replace the real failure with
its own.

The record comes from the transaction owner, not from the reporting step:
`applyRelease` binds its `catch` and writes
`journal.failure = {target, stage, at, error_name, reason}` before re-raising with
the original error attached as `cause`, and `deploy_server.py` writes the same
shape on its `failed-reconciliation-required` path. `reason` is redacted by value
against the API token, the referenced secret values and the whole secret bundle
(Cloudflare) or the stage's runtime environment (Server), and bounded to 2000
characters. The journal is retained on the host and copied into the artifact, so
redaction is the condition for recording a reason at all. A run that fails before
the transaction starts has no `failure` block, and the report says so explicitly
instead of implying the journal is complete.

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
`scripts/fork/workflow_lint.py` resolves constant `fromJSON` runner selectors
through the YAML syntax tree before invoking actionlint with the fork catalog.
The upstream workflow/catalog stay unchanged, while the fork check still rejects
unknown labels. This removes the observed upstream custom-label false failure
without hiding misspelled runner selectors from the fork's required check.

The source CI test suite stays hermetic. The separately credentialed Release CI
performs real cloud runtime/schema and installed-image qualification; CD retains
the full provider/public-origin acceptance. Only a completed deployment journal
proves that the public deployment has succeeded. External platform/state changes
after qualification can still fail CD; no source test promises their availability.
