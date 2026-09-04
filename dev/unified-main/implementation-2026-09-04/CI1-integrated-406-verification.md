# Integrated local candidate through 40657792d7

No push, PR, merge, remote resource change or production deployment occurred.
The branch remains `codex/unified-delivery`; origin/main remains the audited
`d238a85af9d9` and upstream integration is still pending.

The root independently reviewed and integrated selected completion cache/history
(`04baf36c50`), process/cache ownership and Node22 SQLite warning handling
(`931f63c716`), Electron brand presentation (`67851331b9`), authenticated queue
attempts/rejected response retention (`78b9ae9913`/`79e22a8121`) and first chat
admission (`40657792d7`). The manifest conflict was resolved by retaining both
the queue retry test and existing PG nested-transform regression; no upstream
file changed.

| Exact range | Actual result |
| --- | --- |
| `0c13c8dca1..67851331b9` | 24 upstream checks pass; seven named fork checks pass with an explicitly reused verified Pyodide cache. Includes fresh CF product cases, Electron two-target tests/builds, Auth, routes, seams and startup. |
| Same 678 tree, empty owned cache | Actual workerd readiness timed out after 166 seconds. Cache stayed empty and all owned child processes were cleaned. No EPERM and no underlying network diagnosis is claimed. |
| `67851331b9..40657792d7` | 24 upstream checks pass; five named fork checks pass: CF product/routes, upstream touch, backend seams and self-host startup. Actual CF common8/recording6/chat8 and merged queue regressions pass. |
| Fresh standard Server image after queue source changes | Not run in these ranges. The prior c465 image source proof is stale for this new backend tree. Independent real model processing was using the Docker memory window. |

All commands use Node22.23.2 and Python3.11 with `UV_OFFLINE=1`. Upstream and fork
runners are sequential for a fixed clean tree. Named fork checks are not a full
manifest pass. The 90-day recurrence ratchet explicitly reports shallow-history
SKIP; this is not historical verification.

Evidence lives in `/tmp/memweft-implementation/ci/`:
`integration-678-{body.md,suggest.log,upstream.log,fork-seven.log,fork-cached.log}`,
`integration-406-{body.md,suggest.log,upstream.log,fork.log,result.json}`.
The cold fixture is `memweft-cloudflare-product.X5t94W/target` in the system temp
folder; its logs and zero-byte Pyodide cache remain available. The older DQ0dMe
failed fixture was removed only after its owner confirmed it was obsolete; logs
and inventory remain in `cloudflare/root-old-fixture-evidence/`.

Separate ongoing evidence: two actual Server recordings reached durable
finalizer completion but memory extraction failed before the native-schema
repair. The repaired canonical extractor has passed real inference separately;
a complete new recording, persisted memory and retrieval still need to finish.
Default backend brand prompts and Electron binary assets are separate packages
in progress. These facts do not qualify all routes, all white-label surfaces,
supported operating systems, signed releases or remote staging.
