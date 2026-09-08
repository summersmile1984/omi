# First fork CI push — 2026-09-09

The target is `summersmile1984/omi`, branch `codex/unified-delivery`. This push
requests Fork Checks on Mac Studio; it does not merge main or deploy production.

## Local findings repaired

- Upstream Python environment sync removes fork dependencies. CI now restores
  the same hash-pinned fork layer used by Server OS, with macOS ARM64 wheels.
  Actual sync/install and all 285 startup tests passed; the workflow behavior
  test covers successful sync and failure before installation.
- Git's repository-local environment leaked from the linked-worktree hook into
  upstream temporary Git fixtures. This failed the AGENTS.md ignored-worktree
  self-test and let a setup fixture change the primary repo's `core.bare`.
  The original false value was restored; branch, remotes and author identity
  were verified. The fork hook now clears these variables before delegating to
  the unchanged upstream pre-push entrypoint. Its real disposable push test
  executes the original failing self-test. All 10 CI behavior tests passed.
- Local checks selected system Ruby 2.6. Existing Homebrew Ruby 4.0.1 passes the
  watch tests (4 tests, 30 assertions); select its bin directory on PATH.
- The line-count gate passes with measured integration metadata: 45 growing
  files match the merged upstream bytes, and one is the existing PG facade.
  The integration range is not a single failure-class instance; individual
  fix commits retain their concrete declarations and regression artifacts.

## Open tracking record: CI-FORK-2026-09-09-1

GitHub Issues is disabled for this fork, so this is the local tracking record
for the first-push preflight hatch, following the existing fork follow-up docs.
Owner: this fork's CI maintenance. Close only after ordinary fork preflight
checks both deployment targets without classifying generated entrypoints as
dead code.

The upstream dead-code scanner only follows `app/lib/main.dart`; it cannot see
the imports added by `app/fork/prepare.py` to the staged main/AuthService. It
flags all seven live `app/lib/fork/identity/` owners. It also scans the ignored
local `firebase_options_dev.dart`, and flags the Windows deployment table,
whose current build consumers still need auditing. Those are not reasons to
delete the fork identity runtime.
Three legacy backend modules (`utils/mimo_pipeline/tts.py`,
`utils/moss_pipeline/pipeline.py`, `utils/moss_pipeline/prerecorded_provider.py`)
also require a separate consumer/retirement audit; their existing tests alone
do not prove production reachability.

For this initial feature-branch push, use the repository's explicit
`PRE_PUSH_SKIP_PR_PREFLIGHT=1` hatch with the local `OMI_PR_BODY_FILE` review
document. This skips the shared upstream PR gate as a whole; it is not a claim
that the dead-code gate passed. The mandatory failure-class artifact ratchet
and every other upstream pre-push step remain enabled. Fork Checks retains its
own upstream-touch, target, identity and product contracts. No rule, baseline,
allowlist or upstream workflow was relaxed. Full main-merge eligibility remains
separate from starting branch CI.

The remaining local push lane hit CPU-budget contention when starting many
backend test files together. Both flagged tests passed individually at 0.19s
under the unchanged 0.30s limit; using four file workers passed the selected
backend lane. OpenAPI, runtime closure, release guards and actionlint passed.

The local Flutter SDK is 3.38.9 / Dart 3.10.8, while the fork CI provisions the
repository's Flutter 3.44.5 pin. Dependency resolution refused to generate code
because `flutter_contacts` requires Dart 3.12. No tracked app source changed.
For this initial push also use the existing `PRE_PUSH_SKIP_FLUTTER_GENERATED=1`
hatch; the fixed-version CI Flutter lane must pass before merge. Do not run an
older formatter or regenerate upstream files to fit the local SDK.
