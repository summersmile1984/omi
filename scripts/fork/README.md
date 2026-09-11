# Fork checks and upstream synchronization

The active fork CI is `.github/workflows/fork-checks.yml` for
`summersmile1984/omi`. Its check definitions live in
`.github/checks-manifest.fork.yaml`; upstream workflow and manifest files stay
unchanged.

Which upstream workflows are enabled is repository state, not tree state, so it
is declared in `config/repo-state.fork.json` alongside the checks that gate
`main` and the deployment environment protection. `scripts/fork/check_repo_state.py`
runs in every lane: offline it checks the declaration against the tree, and in
GitHub Actions it also compares the registered workflows with their live
enabled/disabled state. The ruleset and environment halves need an
administration credential a `GITHUB_TOKEN` does not carry, so
`scripts/fork/apply_repo_state.py --verify` owns them and an operator runs it.

## Running checks

```sh
# The dedicated Fork Checks lane, including selected native checks on macOS:
scripts/fork/preflight --base origin/main --fork-only

# Combined upstream PR contracts and fork checks (the default full gate):
scripts/fork/preflight --base origin/main

# Inspect the portable CI selection without executing tests:
backend/.venv/bin/python .github/scripts/run_checks.py \
  --manifest .github/checks-manifest.fork.yaml --lane ci \
  --base origin/main --platform linux --output json
```

The wrapper prefers the existing pinned backend interpreter and puts it on the
child-process PATH. Install the Node 22 and component dependencies used by the
workflow before exercising their tests. `--fork-only` selects a named workflow
lane; it does not certify the separate upstream gate or production deployment.
The child `python3` shim executes the original venv interpreter path; relocating
it as a symlink loses virtual-environment package discovery on macOS.

After `make setup-backend`, install the fork runtime layer with
`uv pip install --python backend/.venv/bin/python --no-deps --require-hashes -r backend/requirements-fork.txt`.
The upstream sync removes packages outside its lock. The fork file supplies
hash-pinned CPython 3.11 wheels for Linux amd64 and Mac Studio ARM64, and CI
installs it after every upstream environment sync and selects that interpreter
for subsequent Python checks. Both jobs bootstrap Python 3.12 through `uv` in
the runner account; this avoids the non-relocatable macOS `setup-python` archive
requiring `/Users/runner/hostedtoolcache` on a self-hosted Mac.

## Runner and event routing

For linked-worktree pushes use
`git -c core.hooksPath=scripts/fork/git-hooks push`.
The fork hook removes Git's exported repository-local variables before calling
the unchanged upstream single-flight/pre-push checks. Otherwise nested fixture
repositories can read or modify the caller's Git configuration, and the
AGENTS.md self-test fails despite valid ignored-path behavior. The override is
per command; it disables no check and changes no shared hook installation.
The CI behavior test performs a real disposable push through this hook and the
upstream self-test with an inherited Git directory, work tree and index.

Pushes to `main`, `codex/**`, and `sync/**`, plus manual runs, use
`[self-hosted, macOS, ARM64, mac-studio, memweft]`. Pull requests targeting `main`
use disposable `ubuntu-latest` and, when selected, `macos-26` hosts. The primary
job runs only in `summersmile1984/omi`. Checkout does not persist the GitHub token.

Server OS product checks run actual Linux Docker containers; Cloudflare checks
run local Wrangler/workerd, normal migrations and product HTTP/Queue contracts.
The portable selector uses `--platform linux` on both hosts, keeping macOS-only
tests in their own downstream job. Native checks use Xcode 26.6: Mac Studio's
`/Applications/Xcode.app`, or the hosted image's `/Applications/Xcode_26.6.app`.
No workflow starts or replaces a production desktop app.

Normal CI uses controlled inference and requires no MiMo or Cloudflare secrets.
The explicitly selected live MiMo/Ollama lane remains documented in
[`deploy/cloudflare/contracts/README.md`](../../deploy/cloudflare/contracts/README.md).
CI does not deploy Server OS or Cloudflare production targets.

Manual runs now select the complete fork manifest, retaining each job's platform
scope. `.github/workflows/fork-release-prepare.yml` reuses that workflow and
packages frozen Cloudflare candidates plus Linux Server images from one commit.
See [delivery preparation and remaining promotion work](RELEASE.md) for inputs,
artifact formats, local verification and the outstanding production owners.

The three lanes are the three events, and each gets its own concurrency group
(`fork-checks-<ref>-<event_name>`). A manual full run and the post-merge push run
both resolve to `refs/heads/main`; a shared group let the release lane cancel the
merge commit's own record, so a run can now only supersede a run of its own lane.
The workflow declares no `workflow_call` trigger: nothing calls it, and release
preparation validates the CI run through the API instead.

PRs compare against their fetched target branch. Existing-branch pushes use the
event's `before` commit. First pushes and manual feature-branch runs compare the
whole branch with `origin/main`; on `main` itself, manual runs inspect the current
commit against its parent, while the complete-manifest selector forces all
eligible checks independently of that diff. An unavailable base fails instead
of selecting no checks. `test_ci_diff_base.py` executes the workflow shell in temporary Git repos
and validates the real manifest, including relocated backend test references.

## Upstream boundary

`check-upstream-touch.py` compares against the common ancestor of the checked
head and fetched upstream tip. This pins the policy to the upstream code actually
incorporated in that head. Later upstream changes cannot create false fork edits
or hide a forbidden fork edit. The CI checkout and upstream fetch retain history
so this ancestor can be resolved. A history without a shared ancestor fails.

The invariant is a state, not a diff, so the manifest runs the guard in
`--aggregate` mode: the files it examines are the complete divergence from that
ancestor, not the event's commit range. A diff-scoped base would inspect only the
commits inside its range, so a violation that landed in an earlier commit would
stay invisible to every later run. `--aggregate` needs `upstream/main` fetched
locally; when the ref is unavailable the guard exits 2 and prints the fetch
command, because a run that cannot apply the policy must not report success.

The existing allowlist budgets and forbidden upstream paths still apply. CI runs
this manifest check before provisioning expensive dependencies. Its tests cover
an upstream tip advancing after a clean sync, upstream independently adopting a
still-unmerged fork edit, a violation committed before the event base, and a
missing upstream ref.

Weekly synchronization is separately owned by `upstream_sync_plan.py` and
`fork-upstream-sync.yml`; it must produce a regular merge, never squash or reuse
an unreviewed conflict resolution.
