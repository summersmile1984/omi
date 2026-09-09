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
