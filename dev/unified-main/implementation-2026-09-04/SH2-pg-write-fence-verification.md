# Completed-account PostgreSQL write fencing

The local SH-2 candidate deleted account rows and published a minimal receipt,
but ordinary request/background writers could create rows again. The real
`database.users.record_user_platform` and `record_client_device` paths recreated
two rows for a completed synthetic UID with the old unrestricted SQL policy.
An actual earlier SERIALIZABLE transaction continued to report no deletion
marker or receipt after a separate transaction completed the wipe.

The selected runtime now has one typed facade mutation boundary. It admits old
and proposed owners using the existing completion receipt and holds a shared
account advisory lock on the actual SQL transaction until commit. The provider
fence reads the same authority through a fresh bounded read-only connection,
before entering an external call. No new schema or lifecycle store is added.

PostgreSQL's [transaction isolation contract](https://www.postgresql.org/docs/16/transaction-iso.html)
explains why SERIALIZABLE retains the prior snapshot and a READ COMMITTED query
sees newly committed state. Its [advisory-lock contract](https://www.postgresql.org/docs/16/explicit-locking.html#ADVISORY-LOCKS)
defines transaction-scoped lock lifetime. Database rollback after an external
effect would be too late; the new read occurs before that effect.

## Local behavior

- Formal `backend/test.sh` with `BACKEND_UNIT_TEST_FILE_LIST` selected 17 new
  policy tests and 14 existing provider/deletion tests; all passed. The new
  tests execute eight real facade mutation entry paths, preserve an unmigrated
  principal, reject owner laundering/ambiguous identities, check exact control
  exemptions and suppress SQL effects on authority failure. They use a
  controllable connection seam, not a simulated PostgreSQL isolation claim.
- `backend/.venv/bin/python -m pytest -q firestore_pg/tests/test_deletion_write_fence.py`
  passed six tests against owned disposable PostgreSQL on host port 55478.
  The real old producer recreated two rows; the selected policy retained zero.
  The actual old snapshot still returned no receipt while the fresh read saw
  completion; external effect count stayed zero and late SQL was rejected.
  A wipe could not pass an uncommitted writer and succeeded after its commit.
  Whole-batch rollback, old metadata owner checks and another user's data
  passed. The fresh reader worked while all 15 writer connections were held.
- The existing actual deletion-worker contract also passed with the new SQL
  policy and exclusive lease. Billing, Auth and unrelated provider operations
  were controlled by that existing test; it does not prove their live erasure.
- Independent Cloudflare-agent source review found no blocking defect in these
  bounded boundaries. It explicitly did not approve a release or count the
  external call seam as a live provider transaction.

Logs and controller inputs are in `/tmp/memweft-implementation/pg-write-fence/`.
`unit.log` and `live.log` hold these results. The private fixture config is mode
0600 and contains only synthetic local credentials; do not paste it into logs.

## Standard Linux image

The unchanged upstream `backend/Dockerfile` with its pinned Python 3.11.10 base,
then `deploy/self-host/Dockerfile` with `DEPLOYMENT_STAGE=local`, built
`memweft-server:pg-write-fence` for Linux AMD64. Its OCI index is
`sha256:958a23a9f860de10e3a58a394029d929672b2072a0d655b32244393abf981573`.
The labels identify `3b1d6f70cb-local-pg-write-candidate`; this is a local
uncommitted candidate image, not a published release revision.

`run_image.py` ran a controller script over stdin with no source mount. All
seven changed production module hashes matched the image's own files. Actual
request side effects left a completed root/device namespace empty and retained
another account. An actual old SERIALIZABLE snapshot saw no receipt while the
fresh authority rejected both the external call seam and a late SQL write.
The in-flight SQL writer blocked a wipe through commit, then the wipe succeeded.
Synthetic image-proof subjects were removed. See `source-hashes.json`,
`base-build-final.log`, `image-build-final.log` and `image-proof.log`.

This executes the selected production PG/registry/provider-fence modules in the
standard image. It does not claim an additional full ASGI/model startup or live
Auth/MinIO/Qdrant erase run for this package. Those prior packages have their own
evidence. Formal manifest results are recorded in the local commit message;
the startup lane discovers the new hermetic tests in both local and CI lanes.

## Limits

This closes terminal-receipt SQL writes and stale-snapshot provider admission.
Existing active-marker policies remain responsible for pre-completion access.
It does not infer ownership for arbitrary unregistered/non-UID data, verify every
external family, or resolve a lost connection after a provider accepted a write.
The additional pool reads the existing authority in the same database and has
bounded capacity; it is not a replicated state cache. No remote database,
production process, release pointer, push, PR or merge was changed.
