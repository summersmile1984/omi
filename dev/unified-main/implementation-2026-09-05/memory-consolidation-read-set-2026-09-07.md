# Consolidation read-dependency boundary — 2026-09-07

Failure-Class: FC-consolidation-rejection-context-drift

The real native-intake → hidden negative feedback → candidate retrieval → LLM
adapter → D1 apply path failed locally with `memory_apply_target_changed`.
The original context owner correctly admitted hidden owner rejection examples,
but D1 put every hydrated context row in the interactive **write** guard. That
guard correctly requires active/unlocked mutation targets, so a valid read-only
example prevented unrelated source writes. This is a migration defect, not an
observed Cloudflare service outage.

Migration 0182 adds a read-only snapshot column/transaction guard. The canonical
plan's actual changed IDs retain the original write guard; other hydrated items
use the read guard, including the expected status and lock value. Neither read
membership nor a hidden example authorizes graph or memory mutation. All checks
share the canonical journal/apply transaction. Existing writers have an empty
read set by default. No upstream code, default prompt or model setting changed.

## Evidence

The existing Core pytest lane discovers
`tests/test_memory_consolidation_dependencies.py`. It executes actual native
routes and all App migrations in SQLite, with controlled vector/provider seams:

- Hidden, locked owner feedback and an ordinary retrieved candidate remain
  byte-for-byte unchanged while an unrelated source decision commits.
- Content/lock/status/deletion races on either read dependency abort the whole
  transaction; the source stays pending.
- A candidate locked after retrieval cannot become a replacement write target.
- `test_memory_consolidation_planning.py` additionally executes multi-batch
  consolidation with hidden rejection feedback and verifies the original model
  messages and preserved candidate groups.

Focused command (from the worktree):

```sh
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests/test_memory_consolidation_dependencies.py deploy/cloudflare/python/api-core/tests/test_memory_consolidation_planning.py
```

Combined initial result: **17 passed**. Complete suite, formatting and old-code
replay results are recorded in the commit body and retained local evidence.
Old-code replay installs the actual pre-fix apply function into the test helper,
without editing the runtime source or disabling the SQL guards.

Initial fixture corrections are not production failures: changing only a lease
expiry/generation column violated existing JSON/column constraints; a locked
candidate is excluded by the current projection view, so the replacement test
locks it **after** retrieval. The standalone dependency test now declares its
own source import path. None of these corrections relaxed product assertions.

This proves local native route/SQL behavior. It does not prove hosted Pyodide/D1
execution of migration 0182 or qualify an Eddy production release.
