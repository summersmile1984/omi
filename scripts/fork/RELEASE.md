# Fork delivery preparation

The repository is `summersmile1984/omi`. Both deployment targets consume the
same commit and brand/stage profile. Feature branches can prepare candidates;
production promotion is a separate operation after main integration and its
release qualification.

## Executable preparation

`.github/workflows/fork-release-prepare.yml` is a manual artifact workflow. It
calls the existing Fork Checks workflow at the same commit, then invokes
`scripts/fork/prepare_release.py`. Manual Fork Checks runs select every check in
the fork manifest, even on main with no relevant diff. Push/PR runs keep their
diff selection. The upstream runner still owns manifest validation, platform
filtering and check execution.

Inputs are a repository `brand` (for example `eddy`), a `stage` (`beta` or
`production`), and the existing CF3 `inventory_json`. The inventory contains
resource IDs, routing configuration and secret references, never secret values.
No deployment credential is passed to the preparation job. Mac Studio builds
the currently supported Server platform, `linux/amd64`, through Docker.

After the workflow is published on the fork, dispatch it from the desired
branch in Actions. A local equivalent, after provisioning the dependencies in
`fork-checks.yml`, is:

```sh
backend/.venv/bin/python scripts/fork/prepare_release.py \
  --brand eddy --stage production \
  --inventory /private/inventory.json --output /private/new-delivery
```

Add `--plan` to inspect the exact commands without building. Preparation
requires a clean committed checkout and a fresh output directory outside it.
The workflow and commands have local behavioral/lint coverage; this new
end-to-end artifact workflow has not yet been executed on GitHub.

Preparation delegates Cloudflare qualification, both Web builds, migrations,
nine Worker compilations, frozen dry-runs, and local Wrangler/Docker product
regressions to the existing `release.mjs prepare` owner. It then builds the
upstream Server runtime and fork backend/auth/LLM/Web images. The Web image
consumes the candidate's already-built Server Web artifact. It verifies actual
Docker image IDs, Linux amd64 architecture, and source commit/tree labels.
Any failed command, dirty source, source change, or wrong image prevents a
delivery receipt. Model weights and production environment files are not
included in these image archives.

The resulting artifact contains:

| File | Contents |
| --- | --- |
| `cloudflare.tar.gz` | Original immutable candidate: nine Workers, assets, SQL, resource inventory, both Web targets and qualification logs. Tar preserves hidden modules and metadata. |
| `server-images.tar` | Docker image archive for backend, auth, LLM runtime and Web. |
| `source.tar.gz` | Exact committed source, without local ignored credentials. Production operations still need a Git checkout at that commit. |
| `delivery.json` | Commit/tree, brand/stage, candidate digest, actual image IDs and SHA-256 of each archive. `release_ready` remains false. |

The preparation artifact is retained for three days; it is a candidate, not the
durable release registry. No registry push, SSH, remote SQL, Worker publication,
production container restart or traffic switch occurs. CI uses controlled
inference; existing local MiMo/Ollama verification remains a separate provider
test. The default prompts and production model profiles are unchanged.

## Promotion work still required

| Target | Existing owner | Remaining CD connection |
| --- | --- | --- |
| Cloudflare | `deploy/cloudflare/scripts/release.mjs` (`check`, `provision`, `apply`, `recovery-plan`, `restore`) | Implement the missing `CF-4` and `CI-1` executable product qualifications, then supply scoped account credentials and run the existing candidate/deployed observations. Bind the exact candidate digest to the transaction journal. |
| Server OS | `deploy/self-host/build-images.sh`, `operations.sh`, Compose and migration/snapshot owners | Boot-test the exact delivered images; publish to the chosen registry and record registry digests; add an operation that pulls those digests and runs the existing migration/health sequence without rebuilding. Select the actual host, registry, TLS origins and runtime environment. |

The current Server `operations.sh start` always calls `build-images.sh`. It is
therefore not yet the immutable-image CD entrypoint. Do not call it a deployment
of the previously accepted archive. Its backup/restore tooling and migration
order remain the owners to reuse when implementing promotion.

The current Cloudflare tree has `qualify-prior-schema.mjs` for first-release
empty-schema qualification, but lacks `qualify-product.mjs` and
`contracts/deployment/qualify-dual-target.mjs`. Existing-version/new-schema
rollback qualification also remains pending. These checks must execute their
real product surfaces; approval flags and local test receipts cannot replace
them. See `deploy/cloudflare/release.md` for the exact runner protocol.

As observed on 2026-09-09, this fork has `development` and `prod` GitHub
Environments, both with no protection rules; repository Secrets and Variables
listings are empty. The future production jobs should bind the selected
Environment, an exact source/candidate and target-specific concurrency with
`cancel-in-progress: false`. Credential selection belongs to those jobs, not
this build job. No environment or credential setting was changed by preparation.

## Verification record

Run `backend/.venv/bin/python scripts/fork/test_ci_diff_base.py` and
`backend/.venv/bin/python scripts/fork/test_release_prepare.py`. The existing
local/CI manifest lane runs both. The latter executes the real selector with no
diff and tests orchestration success, build failure, wrong image architecture
and dirty source through a controlled command seam. It does not claim a real
Docker delivery build or a deployed product smoke. Workflow syntax/shell checks
run through the fork's actionlint manifest entry, with its own runner-label
configuration so upstream configuration stays unchanged.

GitHub run [34269817829](https://github.com/summersmile1984/omi/actions/runs/34269817829)
passed on `0aaf0c03f3`: four portable checks and two macOS checks, ending
2026-09-09 04:00 China time. It was a diff-scoped run, so Server/Cloudflare
business contracts were not selected. The subsequent manually triggered
[34301227612](https://github.com/summersmile1984/omi/actions/runs/34301227612)
compares the whole branch against main using the already-published workflow.
Its result is independent of these new, locally prepared workflow changes.

The whole-branch run subsequently failed in `fork-cloudflare-routes`:
`screen-frame-source.test.mjs` exceeded Vitest's 5-second test deadline.
The Cloudflare suite reported 1107 passed / 1 failed. Before that, the actual
Cloudflare and Server product lanes, Electron/Flutter identity, build context,
auth contracts and Web build checks passed. Remaining manifest checks and the
dependent macOS job did not run; this is not a full green release result.

The screenshot case took 6.739 seconds, including Python startup/imports and
codec execution, against a 5-second deadline. On the same Mac Studio and Node
22.23.2, unchanged-code reruns passed: isolated 0.770 seconds, full-suite
0.813 seconds, and full-suite with four workers 0.438 seconds. Those reruns
establish timing variability, not the exact cause of the original host delay.
The fix bounds Vitest to four workers and gives only that subprocess integration
15 seconds; all upstream behavior assertions and other test deadlines remain.

## Proposed Server endpoint on the CI machine

Mac Studio can host the persistent Server deployment as well as the runner.
The application still runs in the Server OS Linux containers. The present
locked runtime is Linux amd64, so Apple Silicon uses Docker's emulation; this
proposal does not claim a native ARM64 runtime-lock qualification.

Public traffic follows this path:

```text
HTTPS/WSS client → Cloudflare hostname → named Tunnel
  → persistent cloudflared → local reverse proxy → Server Compose services
```

The connector makes outbound connections, so the machine needs no public IP or
inbound router port mapping. One named tunnel can publish several hostnames.
This does not require an application Worker in front of the Server target.
The selected operator-owned zone is `smartipproxy.com`, verified active in the
Cloudflare account on 2026-09-09. The DNS search for `eddy` returned no records.
Both targets now have separate names in `brand/eddy/manifest.yaml`; Workers
custom domains and Server Tunnel hostnames must remain disjoint. No tunnel,
DNS record, traffic switch or daemon was created by this configuration update.

Selected production endpoints (binding awaits the corresponding deployment):

| Surface | Cloudflare target | Server OS target |
| --- | --- | --- |
| API / recording / MCP | `eddy-cf-api.smartipproxy.com` → Edge Worker | `eddy-server-api.smartipproxy.com` → Tunnel → backend gateway |
| Authentication | `eddy-cf-auth.smartipproxy.com` → Auth Worker | `eddy-server-auth.smartipproxy.com` → Tunnel → Better Auth |
| Web / share | `eddy-cf.smartipproxy.com` → Web Worker | `eddy-server.smartipproxy.com` → Tunnel → frozen Web container |
| Authorized objects | CF API host → existing authenticated object routes | `eddy-server-objects.smartipproxy.com` → Tunnel → MinIO S3 endpoint |

Beta uses the separate prefixes `eddy-cf-beta` and `eddy-server-beta`, e.g.
`eddy-cf-beta-api.smartipproxy.com`; local profiles remain on loopback.
The manifest's `deployments.<target>.<stage>` overrides feed both artifacts
prepared from the same commit. For native CF delivery, the resource inventory
must select `routing.mode: custom_domains`; the existing resource renderer
derives Worker host bindings from that resolved profile. Server names belong
only in the named Tunnel's public ingress and gateway configuration. Keep
auth `/internal/` routes private and disable proxy buffering for streaming/MCP.

The same public HTTPS origins must be rendered into the Server brand profile,
Better Auth issuer/audience, CORS, share links and signed-object URLs. Preserve
Host and forwarded HTTPS information. Databases, MinIO console, Docker API and
the Ollama embedding endpoint remain internal. With Docker Desktop, containers
can reach a native host service through `host.docker.internal`; the Ollama
listener must be reachable from that VM and restricted from public ingress.
Local LLM/ASR/TTS continue to use the selected MiMo configuration, while
embedding uses Ollama; this topology does not rewrite prompts or select a new
provider implicitly.

Use a named tunnel with a stable hostname. Cloudflare documents WebSocket
support; SSE requires the origin's `Content-Type: text/event-stream` and an
unbuffered reverse proxy. Quick `trycloudflare.com` tunnels do not support SSE.
Acceptance must exercise login/refresh, streamed chat, recording WebSocket,
MCP, signed objects and reconnects through the actual hostname. Tunnel traffic
is still subject to Cloudflare proxy upload/connection limits.

The connector, gateway and state volumes are persistent services independent
of Actions jobs. The Server CD operation should import or pull the exact
accepted image identities, record the previous versions, back up persistent
data, run the existing forward migration sequence, replace application
containers, then smoke-test through the public hostname. Do not put production
data or checkout under the runner's `_work` directory. Do not attach deployment
teardown to a CI job's cleanup. A version rollback must first establish schema
compatibility; it must not silently reverse a database migration.

For shared physical hardware, isolate CI and persistent deployment into
separate VM/Docker environments and restrict the production engine to the
deployment owner. Separate Compose project names prevent naming collisions
but do not isolate privileges on a shared Docker socket. The existing public
fork's external PR jobs must continue on disposable hosted runners. The host,
Docker runtime, connector and network must stay running for the endpoint to
remain available; Tunnel does not replicate the local database or application.

Native Cloudflare CD has a different request path: clients terminate at the
deployed Workers and Cloudflare storage services. Mac Studio only builds and
publishes those artifacts; it is not an origin required for their availability.
Its publisher already owns frozen D1 migration checks, dependency-ordered
Worker deployment, observed version IDs, readiness and transaction journals.
The pending product qualification and production workflow connection described
above still apply.

References: [Tunnel setup](https://developers.cloudflare.com/tunnel/setup/),
[WebSocket support](https://developers.cloudflare.com/cloudflare-one/faq/cloudflare-tunnels-faq/),
[SSE streaming](https://developers.cloudflare.com/cloudflare-one/troubleshooting/tunnel/),
[Docker host networking](https://docs.docker.com/desktop/features/networking/networking-how-tos/),
[self-hosted runner isolation](https://docs.github.com/en/actions/reference/security/secure-use).
