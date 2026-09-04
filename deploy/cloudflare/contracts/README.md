# Disposable Cloudflare product contracts

`bash deploy/cloudflare/ci/product.sh` is the fork manifest's **local and CI**
entry. Install the existing npm lock with `npm ci --prefix deploy/cloudflare`
and have `uvx` available; `scripts/python-worker.mjs` installs/checks pinned
workers-py and uv, consumes both committed Python locks and rejects lock drift.
The entry builds seven real application Workers and an inference-only Worker,
applies the actual Auth/App migrations into a new local state directory, starts
locked Wrangler/workerd, then executes:

- The same `contracts/deployment/core.py` identity/onboarding/Tasks HTTP suite
  used by the Server OS runner.
- `recording.mjs`, separate public `/v4/listen` (Upgrade Bearer) and
  `/v4/web/listen` (first-frame JWT) PCM recording flows: authenticated
  capture, committed transcript reads, reconnect ownership, cross-user denial,
  explicit finalization through actual Queue/Jobs/Core, derived memories/tasks,
  and denial after logout. This is not the full dual-target recording matrix.
- `chat.mjs`, the current upstream Web `api.ts` get/send/clear functions against
  actual HTTP/SSE, Python model RPC and D1: UTF-8/newline decoding, selected
  session/app history, cross-user 404, provider failure without partial history,
  explicit clear retaining the session, and terminal deletion. Only the actual
  signup-issued token and `/api/proxy` base mapping are injected into the client;
  its requests, responses and SSE parser are unchanged. This is a text-chat
  contract, not a browser UI, tools/attachments or full chat qualification.

For an interactive isolated fixture:

```sh
node deploy/cloudflare/contracts/local-target.mjs \
  --output /tmp/new-owned-cf-target --brand-id local-fixture
```

The parent output directory must exist; the final directory must be new, including
no broken symlink. `metadata.json` contains `api_origin`, `auth_origin`, `target`,
`brand_id`, and `trace_dir`. `--run-core`, `--run-recording` and `--run-chat` select the executable
suites and stop the target afterward. Otherwise SIGINT/SIGTERM stops it. Startup,
commands and teardown share one process-group owner: cancellation prevents later
stages, kills descendants, and never adopts an already-running endpoint. The recording signup driver honors one server-provided, bounded
`X-Retry-After` response when the shared loopback signup rate limit is reached;
that 429 remains in the trace and no auth rule is disabled. Each
command has a five-minute deadline, readiness two minutes, and an interactive
fixture one hour. Logs, synthetic secrets and report files stay in the new private
output directory. The shell prints this path; it retains evidence/state rather
than deleting unknown directories. No Cloudflare remote operations are issued.

`local-config.mjs` consumes current production Worker/binding owners and rejects
unknown owners or unsupported newly introduced runtime primitives. D1, R2, DO,
queues and service bindings are local and uniquely named. It clears remote
credentials from child environments and replaces deployment origins/secrets with
loopback/per-run values. It never consumes a release inventory or approval.
`fixture.json` records source revision/status, npm/Python locks, tool versions,
normal migration files, exact frozen module hashes and configuration hashes.

The ASR WebSocket and structured/text inference provider are controlled; application
routes, Auth sessions/JWT, data ownership, D1 storage and Queue delivery are real.
Wrangler's Node test harness cannot transport an outbound WebSocket upgrade, so
the runner uses its actual CLI with the fixed native-workerd launcher. Transcript
quality, hosted model behavior, remote Vectorize/Images, custom domains, previous
schema compatibility, every product route and client platform remain unproved.
Fresh Python package/cache downloads require network and valid TLS; a verified
local Pyodide cache may be reused via `CLOUDFLARE_PYODIDE_CACHE_DIR`. A cache failure
fails the runner; it never starts a Core stub or returns a substitute success.

These reports always set `release_qualified: false`. They do not implement or
replace CF5's pending complete-product, dual-target or prior-schema qualifiers.
