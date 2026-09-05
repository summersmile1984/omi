# Upstream v0.12.291 local sync verification

Date: 2026-09-05

## Scope

- Upstream target: `upstream/main` at `520cc70ac06d63af818ba5170a27cea0db1cfefe` (`v0.12.291`).
- Local merge commit: `552cb91330597d83ebf31221b06e4162dde3b8d1`.
- Follow-up self-host compatibility commits: `73de63d21c` (feedback schema v7) and `c8414bef6f` (local PTT atomic budget reservation).

The merge initially conflicted only in `backend/utils/llm/clients.py` and
`backend/utils/llm/model_config.py`. Both resolved to the exact upstream
versions. Local model selection remains outside those files through
`backend/fork/patches/llm.py`, so accepting the upstream request-timeout and
route-option changes keeps the merge boundary narrow.

## Checks run

```text
python3 scripts/fork/upstream_sync_plan.py --base HEAD --upstream upstream/main
=> conflicts: []

git merge-base --is-ancestor upstream/main HEAD
=> exit 0

git rev-list --left-right --count upstream/main...HEAD
=> 0 236
```

```text
BACKEND_UNIT_TEST_FILE_LIST=<22-file self-host and PTT selection> bash backend/test.sh
=> exit 0

BACKEND_UNIT_TEST_FILE_LIST=<fork/tests/test_speech_transport.py> bash backend/test.sh
=> 21 passed

FIRESTORE_PG_DSN=<fresh temporary PostgreSQL> ENCRYPTION_SECRET=<test value> \
  backend/.venv/bin/python -m pytest backend/firestore_pg/tests/test_transaction_semantics.py -q
=> 29 passed
```

The 22-file selection covers startup/profile admission, local LLM, storage,
outbox, speech, PTT, queue, auth, capability, PostgreSQL owner and upstream
duration-budget paths. The real PostgreSQL suite exercised a fresh v7 schema
and a simulated v6-to-v7 upgrade that retains an existing `feedback_events`
row while re-registering both feedback collections.

## Limits

This is a local candidate only. No branch was pushed, no PR was opened, and no
Cloudflare resource, Server OS release, signed native client, or production
deployment was created. The full release matrix and remote target qualification
remain required before release readiness can be claimed.
