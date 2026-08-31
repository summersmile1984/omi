# Desktop release historical replay

`.github/scripts/backfill-desktop-release-manifest.py` is the operator handoff
for one retained legacy desktop release manifest. The default mode is a
read-only dry run: it reads the exact legacy manifest, validates the shared v1
contract and detached SHA-256, and emits a content-bound plan. It does not
write D1, change a channel pointer, or claim that production data was replayed.

```bash
ADMIN_KEY='…' python3 .github/scripts/backfill-desktop-release-manifest.py \
  --release-id v1.2.3+10203-macos \
  --output /tmp/desktop-release-plan.json
```

The plan contains only the immutable public manifest, its canonical digest,
the legacy read endpoint, and a plan hash. Credentials are never serialized.
Review the plan out of band, then apply it explicitly through the protected
Cloudflare Edge → Jobs boundary:

```bash
ADMIN_KEY='…' python3 .github/scripts/backfill-desktop-release-manifest.py \
  --plan /tmp/desktop-release-plan.json \
  --target-base-url https://omi-cf-edge-staging.<account>.workers.dev \
  --apply
```

The Jobs executor is disabled unless
`DESKTOP_RELEASE_HISTORY_IMPORT_STAGING_ENABLED=true` and a matching
`secret-key`/`ADMIN_KEY` are present. Migration
`0134_desktop_release_history_executor.sql` records the reviewed plan and one
content-bound apply marker per `release_id`. Applying calls API Core's existing
manifest registration endpoint through its service binding, so the existing
immutable v1 validation, digest check, insert-once semantics and no-update/no-
delete triggers remain the destination contract. Retries use the D1 release-id
CAS/marker and never write a channel pointer; channel promotion remains a
separate explicit CAS operation.

This is a staging handoff, not production parity. It does not fetch Firestore
directly, copy release artifacts, promote Stable/Beta, or establish a
production release-pipeline authority. The executor has no account-scoped
payload, so account-deletion fences are not applicable; immutable manifest
triggers and the release-id apply marker are the relevant authority fences.
