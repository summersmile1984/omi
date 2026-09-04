# PostgreSQL onboarding schema admission

The actual Android self-host fixture reached `GET /v1/users/onboarding` with a
valid identity and received 500. Its production owner resolves
`users/<uid>/onboarding_admission/current` dynamically; this collection was
absent from the admitted v4 schema. The standard v4 image reproduced
`SchemaNotCurrent` through `database.users.ensure_backend_onboarding_admission`
for an existing synthetic principal.

The forward-only v5 migration provisions this collection through the existing
schema authority. Versions 1–4 and all earlier physical mappings stay frozen.
No serving request creates tables. The new shared `SchemaFirestore` fixture
executes real dynamic-path owners against the migration inventory; it covers
onboarding and the earlier legal-hold case from `3cddcf6a19`. This is a reused
behavioral fixture in the existing startup lane, not a separate source scanner.

## Verification

- `backend/test.sh` with `BACKEND_UNIT_TEST_FILE_LIST` selected the new owner
  cases and startup contracts: four owner tests and 23 startup tests passed.
  Before the migration change, three onboarding cases failed on the missing
  collection and the existing legal-hold case passed.
- A real v4-to-v5 upgrade retained all 141 registered mappings and their row
  hashes, including a preexisting principal, and the four original ledger rows.
  It added only `onboarding_admission`; repeated migration was a no-op. A fresh
  database admitted the exact v5 static inventory. The standard v4 image rejected
  the v5 database rather than attempting a downgrade.
- The actual FastAPI users router and PostgreSQL served onboarding GET/PATCH
  with 200: repeated requests reused admission, completion persisted, completed
  restoration could not issue another admission, and an unrelated legacy field
  stayed intact. Only the authenticated-principal dependency was overridden;
  this test does not claim another JWT-verifier or Android UI run.
- `python -m pytest -q firestore_pg/tests/test_transaction_semantics.py
  firestore_pg/tests/test_deletion_write_fence.py` passed 32 real PostgreSQL
  tests, including existing transaction/deletion behavior and the new admission
  regression. Pinned Black reported six files unchanged; diff hygiene passed.

The unchanged upstream `backend/Dockerfile` with pinned Python 3.11.10, followed
by `deploy/self-host/Dockerfile`, built Linux AMD64 image
`memweft-server:pg-onboarding`, OCI index
`sha256:2a6e7603958861b42a442aa1d6fde5c53324dc9baf31c37ae2db6f49db4839b1`.
Its label identifies `4e1786fad0-local-pg-onboarding-candidate`. Four production
module hashes matched the candidate. With no source mount, the image executed
the actual FastAPI GET/PATCH path against PostgreSQL with the selected terminal
write policy; admission reuse, completion denial and legacy data preservation
passed. This does not claim a full model/bootstrap or live-provider run.

Commands, image/source hashes and raw output are under
`/tmp/memweft-implementation/pg-onboarding/`: `red.log`, `green.log`,
`live.log`, `preservation-summary.json`, `pg-suite.log`, `old-schema-reject.log`,
`base-build.log`, `image-build.log` and `image-proof.log`. Exact manifest results
are recorded in the local commit message.

Host disk exhaustion interrupted the first Docker attempt before a container
was created. After cache cleanup and Docker recovery, inventory reconciliation
confirmed no pending proof container; the bounded retry produced the results
above. The existing PostgreSQL volume retained its v4 state. No remote database,
main branch, PR, release pointer or production application was changed.
