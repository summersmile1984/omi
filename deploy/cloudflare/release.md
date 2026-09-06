# Current main release candidates and transactions

The release entry is `scripts/release.mjs`. Both npm stage commands invoke its
`prepare` operation: staging maps to profile stage `beta`, production to
`production`. All inputs are explicit CF3 brand/profile/resource inputs; both Web
targets must be configured for the chosen stage. There is no separate target
branch, retired framework publisher, hardcoded account/subdomain, automatic
secret generation, or legacy snapshot compatibility format.

## Local preparation and review

Run from `deploy/cloudflare` with installed frozen dependencies and `make setup`:

```bash
npm run deploy:staging -- --manifest /secure/brand.json --inventory /secure/resources.json --output /tmp/candidate-beta
npm run deploy:production -- --manifest /secure/brand.json --inventory /secure/resources-production.json --output /tmp/candidate-production
npm run release -- check --candidate /tmp/candidate-beta
npm run verify:migrations -- --candidate /tmp/candidate-beta
npm run release -- dry-run --candidate /tmp/candidate-beta --output /tmp/candidate-beta-recheck
```

`--brand <id>` can replace `--manifest`. Output must be new. No local command
calls the Cloudflare API, creates resources, uploads secrets, applies remote SQL
or publishes a Worker. The fixed Python entry still enforces workers-py 1.16.7,
uv 0.12.3 and the existing Wrangler/workerd locks, including when an explicitly
installed pywrangler executable is selected. Offline cache/TLS restrictions are
reported as failures; the publisher does not upgrade the lock or disable TLS.

Preparation runs the real backend route inventory, CF typecheck/full Vitest,
Core/AI pytest, untouched Web checks and fork client/builder checks. It builds the
same stage's Server OS and Cloudflare Moonshine Web artifacts, renders the CF3
resource bundle, executes the two SQL fixtures, then performs nine source
compilations and nine additional frozen `--no-bundle` dry-runs. The nine deploy
configurations reference only copied module/assets files. Python dependencies are
prepared using the fixed Python entry; publication consumes those same frozen
modules via the locked Wrangler runtime, with no package resolver at apply time.

`candidate.json` records:

- Exact base commit/tree, actual source file hashes (including dirty/untracked
  release source), both rendered profiles and Web build manifests.
- Resource plan digest, all SQL filename/content hashes, source dependency lock
  hashes and actual tool versions.
- Every Worker/config/assets file, Bun artifact file and qualification-log hash;
  deploy and rollback dependency order comes from real service bindings.
- Previous/deployed versions as **pending**, never fabricated version IDs or
  inferred absence. `local_verified=true` is separate from `release_ready=false`.

`check` recomputes source and artifact identities, rejects extra/missing/changed
files and symbolic links, and validates SQL bytes through the frozen file map.
Changing source, profile, resource ID, SQL, assets or locks requires a new prepare.
Moving to a new commit (including integration/cherry-pick) also requires a new
candidate. A candidate made from a dirty source remains precisely identified but
does not qualify the later committed source automatically.

The source-level tests run inside the existing local/CI `fork-cloudflare-routes`
lane's full Vitest suite. They replace the retired publishers' private snapshot,
health, Next-generated-file and migration helpers with the actual release owner.
The real incident is the 2026-09-04 current-main audit: the publisher still called
removed Next/vinext commands, and production could mutate resources/migrations
before building Web. These are behavioral owner tests, not a new source scrape
or duplicate deployment registry.

## Admission still pending

The current tree cannot publish while CF-4 and CI-1 remain unimplemented product
owners. First-release schema qualification is implemented; retained-version/new
schema compatibility still requires its executable harness. These are execution
contracts rather than operator approval flags. The CLI reports absent fixed
runner paths before remote apply/restore:

| Owner           | Required executable                                    | Contract                                                                                                                                                                                                             |
| --------------- | ------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CF-4            | `deploy/cloudflare/contracts/qualify-product.mjs`      | Actual enabled route/provider, identity, HTTP, WebSocket, MCP, share/object and persistence contracts, including error paths                                                                                         |
| CI-1            | `contracts/deployment/qualify-dual-target.mjs`         | Same candidate's Server OS/CF product conformance, including the known Tasks 422/400 divergence and platform/brand matrix ownership                                                                                  |
| CF/schema owner | `deploy/cloudflare/contracts/qualify-prior-schema.mjs` | First-release absence/empty authority, frozen SQL execution and deployed catalog/ledger checks implemented; an existing prior Worker or restore phase is refused until retained-version compatibility is implemented |

Each receives `{candidate_directory, candidate, observations}` on stdin and must execute its acceptance
surface. Exit 0 plus JSON `{schema_version:1,candidate_digest,observation_digest,
cases:[{id,result:"pass"}]}` is required. Cases must be nonempty and all pass.
`observations.release_phase` is `candidate` before mutation, `deployed` after
version/readiness checks, or `restore` for the retained-version/new-schema gate.
Candidate phase tests the frozen artifacts; deployed phase must exercise the
actual newly observed product endpoints. The deployed input retains prior
versions separately. A before-deploy result cannot stand in for deployed proof.
The release records runner hash, result hash and observation digest. These are
fixed source paths in the candidate, not arbitrary shell commands or downloaded
proofs. Adding a runner requires the same repository tests/review gate as its
product capability; a file that merely emits a passing object does not implement
this contract. No `approved=true`, unsigned operator attestation, old staging
deploy, readiness 200 or local stub test supplies that evidence.

The directory comes from the release owner's validated candidate, not a proof
file. Each runner reopens it through `qualification-context.mjs`, verifies its
source/artifact bytes and rejects a different stdin candidate. The schema runner
re-observes every Worker and the exact D1 identities. Candidate-phase acceptance
requires genuine Worker absence plus empty business catalogs and migration
ledgers. It executes the candidate's copied `sql/` files through the existing
SQLite migration fixture, preserving its unmigrated principal/session/task checks
and reentry check. SQL from the working checkout cannot substitute for frozen
bytes. Deployed-phase acceptance also requires the candidate/artifact annotations,
the complete migration ledger, exact executed schema catalog and a clean
[`PRAGMA foreign_key_check`](https://developers.cloudflare.com/d1/sql-api/sql-statements/).
It emits explicitly named `first-release.*` cases. An observed retained Worker,
nonempty initial authority, schema drift or denied observation is a failure;
this first-release proof makes no claim about rollback to a previous version.

## Future explicit remote operations

The following commands are implemented but were **not executed remotely** for
this package. They require an exact `--authorize <candidate-digest>`, a scoped
`CLOUDFLARE_API_TOKEN`, and any referenced secret values in the process environment.
The account ID always comes from the candidate. Application secrets are written
only to a private temporary file for one deployment and removed afterwards;
raw process/API output and credential values never enter the journal.

```bash
npm run release -- provision --candidate /tmp/candidate-beta --journal /secure/provision-transaction --authorize <candidate-digest>
npm run release -- apply --candidate /tmp/reprepared-beta --journal /secure/release-transaction --authorize <new-candidate-digest>
npm run release -- recovery-plan --candidate /tmp/reprepared-beta --journal /secure/release-transaction --authorize <new-candidate-digest>
npm run rollback:staging -- --candidate /tmp/reprepared-beta --journal /secure/release-transaction --authorize <new-candidate-digest>
```

Provision creates only absent explicit D1/R2/Queue/Vectorize resources, preserves
existing dimensions/metric and requires actual create-response IDs plus a matching
observation before recording ownership. It also preserves the three `created_at`
Vectorize metadata indexes and one-day assets lifecycle prefixes. Created D1 IDs
are written into `journal.result_inventory` as they become known. Reprepare using
that inventory before publishing; placeholder IDs are never deployed. Existing
D1 names must already match explicit inventory UUIDs. Asynchronous metadata
creation may require later observation; a failed/unknown result never triggers
an automatic duplicate mutation.

Eddy's first provisioning attempt on 2026-09-05 created all 18 data resources,
then stopped after the first metadata-index creation: the live API reported
`indexType: "Number"`, although its [documented response](https://developers.cloudflare.com/api/resources/vectorize/subresources/indexes/subresources/metadata_index/methods/list/)
uses `"number"`. The adapter now accepts the explicit lowercase/title-case
values for the three supported types and still rejects a different or unknown
type. The original journal remains reconciliation evidence. Continue with its
observed D1 identities, a newly prepared candidate and a fresh journal; do not
replay the failed transaction or recreate an already observed index.
The next creation exposed asynchronous propagation: the creation process exited
0, its immediate GET was empty, and a later read confirmed the index. Following
one creation the adapter now polls authoritative absence at most ten times,
two seconds apart. It never repeats the mutation; transport, permission and
type errors stop immediately, and exhausted observation leaves reconciliation
required. These behavioral cases run in the existing full Vitest release lane.

Apply observes resources, exact 100% prior Worker versions, account subdomain or
active zone/custom-domain ownership, required policies and secret references. It
executes the fixed product/schema qualification runners, validates the remote D1
migration-name ledger as an exact prefix and then applies frozen SQL. D1's ledger
alone does not attest historical SQL content. It deploys dependencies in order
with `--no-bundle --strict`, a random transaction tag and candidate/artifact
message, verifies the resulting version annotations, and finally checks both
readiness JSON envelopes and all active versions again. Only that completed
transaction can set its own `release_ready=true`; the local candidate stays false.

## Failure ownership and recovery

The journal is mode 0600, written with temporary-file fsync, atomic rename and
directory fsync. Durable `in_flight` intent precedes every remote mutation.
The process lock prevents two commands from sharing a journal. A crash leaves
the lock for operator inspection; no command silently clears a stale lock.

Remote Wrangler commands and each qualification runner have a fixed 15-minute
process deadline. Execution requires a POSIX host (macOS/Linux): the launcher
and descendants own one process group, which is terminated on completion or
timeout. Timeout is a null exit/unknown result, never a success or permission to
retry a mutation; subsequent remote observations retain the same reconciliation
rules. Even valid-looking runner output is rejected unless its process exits 0.

A lost migration response is resolved only by the actual D1 ledger. Exact
transaction version annotations can prove ownership of a Worker even after a
failed publish process, but that state still requires recovery: Wrangler may
have failed after activation while updating triggers/domains. A transport/API observation failure remains
`unknown`, and execution stops. A create response lost before its ID was recorded
cannot establish first-release ownership even if a same-name resource appears.
Preserve the journal and inspect the remote state; explicit adoption belongs in
a newly reviewed inventory and a freshly prepared candidate. Never rerun a used
transaction or guess an absent prior version from 401/403/500/non-JSON responses.

`recovery-plan` is read-only remotely. Restore executes the schema runner again,
checks current version ownership immediately before each action, and restores
only prior observed versions in reverse dependency order. It retains D1 SQL,
R2/Queue/Vectorize data and first-release Workers. Worker deletion, queue-consumer
removal, domain teardown and Durable Object deletion require separate actual
creation/observation ownership; this workflow does not guess their cleanup.
Readiness failure, unknown mutation, concurrent version or missing schema proof
does not count as a successful restore or completed release.

This is a durable operation journal with conservative recovery, not a distributed
atomic transaction across Cloudflare products. Independent operators must not
run competing releases for the same resource inventory; observed drift fails
closed, and no cross-account distributed lock is claimed.

The locked implementation follows Wrangler's [dry-run and prebuilt bundle
contract](https://developers.cloudflare.com/workers/wrangler/bundling/) and
[version/deployment ownership](https://developers.cloudflare.com/workers/versions-and-deployments/).
HTTP response shapes are pinned against the installed Wrangler 4.127.0 client;
controlled HTTP/subprocess tests are local evidence, not proof against a live account.
