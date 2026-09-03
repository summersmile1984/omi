# Fork Rules

Upstream's rules are in [`AGENTS.md`](./AGENTS.md) and the component guides, and
they still apply. This file adds only what is true for **this fork**. It is
fork-owned, so upstream never touches it and it never conflicts on a sync.

Upstream `AGENTS.md` files carry no pointer to this one: upstream keeps them at
their `agents-md-lean` byte ceiling, so even a one-line pointer fails CI. Start
here and in [`dev/unified-main/README.md`](dev/unified-main/README.md).

## 1. Do not modify upstream files

This fork merges `upstream/main` every week. Every upstream file the fork edits
is a future merge conflict; every new file, package alias, or import-time patch
is not.

**The default is zero.** Fork behavior comes from new files, package/module
aliases, entry-point wrappers, import-time patches, build-time generated files,
and environment variables. When that is genuinely impossible (a Swift constant,
a Next.js root config, a C literal), the file goes in
[`dev/unified-main/upstream-touch-allowlist.yaml`](dev/unified-main/upstream-touch-allowlist.yaml)
with a line budget and the upstream PR that will retire it. The allowlist only
shrinks.

Never allowlisted, whatever the reason: upstream tests, lockfiles and dependency
manifests, generated output, bot-written files, upstream CI workflows, upstream
`AGENTS.md`, and formatting-only changes.

Enforced by `scripts/fork/check-upstream-touch.py` via
`.github/checks-manifest.fork.yaml`. The per-platform techniques that replace an
upstream edit are in
[`dev/unified-main/00-upstream-touch-policy.md`](dev/unified-main/00-upstream-touch-policy.md).

## 2. Formatting: two opposite rules

- **The repository's pinned formatter** (`scripts/backend-python-format`, the
  pinned Dart/Swift/prettier versions) — always run it. It moves files toward
  upstream's canonical style and shrinks the conflict surface.
- **A local formatter older or newer than upstream's pin** — never let it write.
  It rewrites upstream files into a shape upstream disagrees with, which is how
  six `web/admin` files became permanent conflicts before 2026-09-03. If the
  push gate's Dart/prettier phase wants to reformat files you did not touch,
  skip that phase with its disclosed hatch and verify the tree is unmodified.

Never commit a change to an upstream file that only alters whitespace or layout.

## 3. Branches

No long-lived feature branches. Deployment targets are directories
(`deploy/self-host/`, `deploy/cloudflare/`) plus a profile; brands are
`brand/<id>/`. Both are matrix dimensions, not branches. Work happens on
short-lived branches off `main` and lands through a PR (regular merge, never
squash).

## 4. Two test lanes

- **Upstream mode** — `backend/test.sh`, `app/test.sh`, the Swift and Windows
  suites, `web-checks`, run with no shim/profile environment set. This proves
  the fork has not changed upstream behavior. Upstream test files are never
  modified.
- **Fork mode** — `backend/fork/tests/`, `app/test/fork/`, desktop `Fork*`
  tests, `contracts/`, run with `OMI_DEPLOYMENT_PROFILE` set. All fork behavior
  is asserted here.

## 5. Weekly upstream sync

Runbook: [`dev/unified-main/06-upstream-sync.md`](dev/unified-main/06-upstream-sync.md).
Log: [`dev/unified-main/sync-log.md`](dev/unified-main/sync-log.md).

Two traps that cost real time on the first sync:

- `git rerere` is enabled here with a large cache and resolves some conflicts
  **without leaving conflict markers**. Take the conflict list from
  `git status --short` (`UU`), never from `grep '<<<<<<<'`, and re-check every
  `Resolved ... using previous resolution.` line against current policy.
- Adding a fork package to a runtime image is not a one-file change: the
  Dockerfile COPY set, the deploy workflow's `paths:` filter, and two closure
  tests must all agree, or CI fails after the push gate passed.

## 6. Commands

```bash
make -f Makefile.fork setup-fork        # keep upstream release tags out of this fork
make -f Makefile.fork preflight-fork    # upstream gate + fork gate
make -f Makefile.fork upstream-touch    # the zero-touch guard alone
make -f Makefile.fork sync-probe        # real conflict count against upstream/main
```

`make preflight` still runs the upstream gate only; `scripts/fork/preflight`
runs both, which is what CI does.
