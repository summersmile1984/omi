# Integrated candidate c465df4e02

The local candidate integrates selected identity profile reads, Redis finalizer
admission/generation publication, Cloudflare selected chat-session ownership and
ordinary Python source staging, and Electron API headers/onboarding graph fixes.
The exact checked range is `0c13c8dca1..c465df4e02`. No remote changes occurred.

`scripts/pr-preflight --lane local --base 0c13c8dca1 --head c465df4e02
--pr-body-file /tmp/memweft-implementation/ci/integration-next-body.md` passed
24 upstream checks. The historical 90-day recurrence check explicitly skipped
because this checkout is shallow; no historical scan is claimed.

Five of eight selected fork checks passed on this same tree:

- Server product core: a newly built standard Linux AMD64 base image
  `memweft-contract-7acfb8225f20-base`, with all tracked backend Python files
  verified against actual image bytes; four fixture contracts and the common
  eight public identity/onboarding/Tasks cases passed. The fixture then removed
  its owned Compose containers/network. Runtime inference was controlled or
  disabled in this bounded suite; this is not model/finalizer qualification.
- Electron identity: 12 core tests, three preparation tests, seven staged tests
  for each target and two complete staged builds passed under Node 22.23.2.
  Frozen dependencies were reused only after exact package, lock, workspace and
  npm configuration hashes matched. The initial attempt without installed
  dependencies failed before tests; it is retained as failed setup evidence.
- Upstream touch, backend seams and the merged startup suite passed. The
  manifest conflict resolution preserves both queue and nested-PG discovery.

The complete fork manifest did **not** pass. Its first CF fixture failed before
readiness because its default Pyodide cache directory did not exist. After
workerd exited, the process-group cleanup raised EPERM and masked that earlier
startup failure. The AUTH suite also failed: the successful identity-import CLI
emits a builtin SQLite ExperimentalWarning on CI's Node 22, while the test
required empty stderr. The route suite was not reached. Separate repairs and
their fresh-target evidence are in progress; passing package tests under Node
24 are not substituted for this Node22 result.

The existing Server fixture evidence is under
`/private/var/folders/v5/vwp83hn54v75tx3l7kqjdp7w0000gn/T/memweft-product-contract.10Czbl/server`.
Integrated commands, results, dependency hashes and failed attempts are under
`/tmp/memweft-implementation/ci/integration-next-*`; the JSON result records
passed, failed and unexecuted checks separately. Root also independently passed
the profile36, queue3 and Python-source8 focused tests. Its first Python-source
command mistakenly used Node's test runner; the corrected Vitest run is the
passing evidence, not that failed invocation.

A new read-only remote check still finds fork main `d238a85af9d9` and upstream
`176a02fe0d95` (`v0.12.286`). The local candidate/upstream unique commit counts
are 207/386, and merge-tree still reports three conflicts: Makefile,
backend/testing/desktop_beta_admission/run.sh and backend/utils/llm/model_config.py.
The probe changed no checkout; upstream has not been merged. Full client OS,
distribution, hosted inference and release qualification remain separate work.
