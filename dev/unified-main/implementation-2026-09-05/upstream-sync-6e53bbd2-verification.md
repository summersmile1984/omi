# Latest upstream local sync verification

Date: 2026-09-05

## Scope

- Previous upstream merge: `520cc70ac06d63af818ba5170a27cea0db1cfefe` (`v0.12.291`).
- Refreshed upstream base: `6e53bbd2ece5445449b6ff48fbed20c802106579`.
- Local merge commit: `453b2e311eff4373c317539aae67bb2a55853ebf`.

The refreshed upstream base added fourteen commits. They include the transcript
reader-drag fix, the summary-content and empty-day recap corrections, and the
free-tier connector-memory policy repairs. The local merge had no conflicts.
No fork-owned or upstream-owned file was manually edited during the merge.

## Checks run

```text
git merge-base --is-ancestor upstream/main HEAD
=> exit 0

git rev-list --left-right --count upstream/main...HEAD
=> 0 241

python3 scripts/fork/upstream_sync_plan.py --base HEAD --upstream upstream/main
=> conflicts: []

python3 scripts/fork/check-upstream-touch.py --base 552cb91330 --head HEAD --upstream-ref upstream/main --json
=> ok: true; upstream_files_changed: 0; violations: []
```

```text
PATH="/tmp/memweft-flutter-3.44.5/bin:$PATH" bash app/fork/test.sh
=> exit 0

PATH="/tmp/memweft-flutter-3.44.5/bin:$PATH" \
  flutter test --no-pub test/widgets/transcript_test.dart
=> exit 0; 17 passed

BACKEND_UNIT_TEST_FILE_LIST=<12-file upstream and self-host selection> bash backend/test.sh
=> exit 0

bash deploy/self-host/ci/product.sh
=> exit 0; fixture5; core8 passed

bash deploy/cloudflare/ci/product.sh
=> exit 0
```

The Flutter stage tested and built the `self_hosted` and `cloudflare` branded
applications, and the changed upstream transcript widget test passed. The selected backend set includes the newly changed recap,
free-tier-memory, conversation processing, self-host configuration, and startup
contracts. The Server OS product runner built a fresh runtime image, applied
normal migrations, then passed the eight shared identity/onboarding/Tasks HTTP
cases. The Cloudflare runner compiled the isolated Worker set, applied local D1
migrations, and passed its core, recording, chat, and public-share contracts.

## Limits

All checks used local fixtures and synthetic identities. This does not create
Cloudflare resources, publish a Worker, open a pull request, or qualify a
remote Server OS, signed client, device integration, or release artifact.
