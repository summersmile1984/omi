# Canonical feedback and fork source boundaries — 2026-09-09

The four unallowlisted upstream edits are removed. The Server feedback repair
remains active through the fork registry; default prompts are unchanged.

## Changes

- Restore `backend/AGENTS.md`, `backend/utils/memory/canonical_memory_adapter.py`,
  `backend/tests/unit/test_memory_apply_store.py` and `docs/docs.json` byte for
  byte from the merged upstream ancestor `c4880cd5f627`.
- `backend/fork/canonical_mutations.py` wraps the original patch builder before
  operation hashing. Both public and internal Server mutation entrypoints are
  registered. Changed arguments, including an empty object, participate in
  the operation identity; the caller's dictionaries are not mutated.
- Move the regression into `backend/fork/tests/test_canonical_mutations.py`.
  It imports production owners, executes the actual apply kernel and covers
  feedback commit, receipt replay and conflicting feedback ID reuse. The
  existing startup manifest discovers it in both local and CI lanes.
- Keep transaction guidance in the fork guide and retain the Cloudflare
  developer page through the fork documentation index. Update the existing
  failure-class prevention artifact to the relocated test.

Failure-Class: FC-canonical-mutation-omits-patch-fields

## Verification

Commands ran from the unified-delivery worktree. Backend commands used its
pinned Python 3.11 environment and `backend/test.sh` file isolation.

| Verification | Result |
| --- | --- |
| `backend/test.sh`, selected fork canonical mutations, original memory apply store and real seam tests | 5 + 45 + 4 passed |
| `backend/.venv/bin/python .github/scripts/run_checks.py --manifest .github/checks-manifest.fork.yaml --lane local --base origin/main --check-id fork-selfhost-startup` | 23 files, 285 tests passed |
| Core Worker `.venv/bin/python -m pytest -q tests/test_jit_trigger_feedback_routes.py tests/test_memory_product_mutation.py tests/test_memory_apply_edit.py` | 56 passed |
| `bash deploy/self-host/ci/product.sh` | Actual Linux Docker services and public HTTP contracts: 16 passed; owned services cleaned up |
| `scripts/fork/test_check_upstream_touch.py` / `scripts/fork/test_ci_diff_base.py` | 16 / 8 passed |
| Upstream-touch guard against the current source snapshot, base `origin/main` | Zero violations; one existing allowed macOS documentation seam |
| Pinned Python formatter, `git diff --check`, `actionlint -shellcheck ''` | Passed |
| Exact private MiMo key scan of changed and untracked files | Passed |

Source snapshot: `eaced1c7405fe6b3ab3e654afd4d55be0c0eeacc`. It is an
unreferenced local verification object created through a temporary Git index;
the branch and normal index were not updated. Local logs and the guard JSON
are in `/Users/macstudio/.codex/eddy-production/source-boundary-fix-20260909/`.

This verifies the repaired source and its relevant local contracts. The Docker
product report remains `release_qualified: false`: it uses controlled providers.
This run did not push, trigger GitHub Actions or deploy production Workers.
The earlier full-branch upstream preflight line-growth metadata failure is a
separate outstanding landing gate; this record does not claim it passed.
