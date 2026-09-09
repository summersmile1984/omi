# Candidate integration entry composition — 2026-09-08

Failure-Class: FC-shadowed-route-handler

At c66e9e3, `entry.py` imported two distinct modules into `integration_router`.
The later app-integration import replaced the Candidate integration binding;
FastAPI mounted the original router twice while the new internal processor was
absent. This was an application assembly defect, not a Cloudflare/D1 limitation.

Three new tests reproduced the defect before the fix: nine duplicate method/path
registrations, 404 after public Candidate create/accept reached internal prepare,
and 404 where internal-authority rejection should return 401. Giving the Candidate
router a distinct import and mount preserves both owners. The runtime registry
guard is reusable across the complete mounted app; the behavioral test executes
`entry.app`, its assertion middleware, actual App SQL and full accept/prepare/
settle/task-read requests. It also verifies the original integration status route.
The guard targets this recorded c66e9e3 instance and the same class recorded for
upstream PR 12697 in the existing failure-class registry.

Entry and control-focused tests passed together: **12 passed in 6.54s**, including
the three previously failing assembly tests. Full final-worktree validation,
which also contains the universal workflow-control feature, passed **1,148 Core
tests** and **1,028 Workers tests in 128 files**. No source-string assertion is
used as proof of handler reachability.

The same ordinary frozen Core/Jobs builds were deployed to isolated Cloudflare
Workers with all 191 App migrations and real Queue bindings. Both integration
receipts completed through the actual Jobs consumer in one attempt. No handler,
provider, dispatcher or ASGI entry was replaced in the hosted fixture. Auth
assertions used synthetic principals; with no default external service, delivery
completed by the original terminal no-op rule. This proves hosted assembly and
the signed service boundary, not live third-party task creation or public login.
The trial passed at `2026-09-07T21:15:50.339Z`; both Workers and all three resources
were observed absent by `21:16:11.191Z`. No production resource was changed.

The temporary driver used ordinary `python-worker.mjs`/Wrangler dry-run, frozen
payload verification, then explicit owned-resource deployment. Before/after logs,
source hashes, version IDs, HTTP observations and cleanup receipts are retained
under `~/.codex/eddy-production/canonical-control-hosted-20260908/` after the final
local commits. Credentials and raw private deploy logs are excluded. This does
not establish CF-4/CI-1 or production-connected macOS acceptance.
