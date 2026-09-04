# AUTH import reconciliation order

The real imported-dot-identity probe exposed a second, independent defect:
`planFirebaseIdentityImport` sorted opaque IDs with JavaScript `localeCompare`,
while its PostgreSQL verifier read `ORDER BY id COLLATE "C"`. A three-UID export
passed validation but rolled back during apply because the two content hashes
differed. The earlier single-user import proof did not cover this ordering.

The planner now compares UTF-8 bytes for both user and account rows. This matches
the [PostgreSQL 16 C collation contract](https://www.postgresql.org/docs/16/collation.html)
using [Node Buffer comparison](https://nodejs.org/api/buffer.html#static-method-buffercomparebuf1-buf2).
Identity values, stored envelopes, SQL writes, and the import receipt schema are
unchanged. The regression is discovered by the existing Auth component command
and the shared AUTH local/CI manifest lane.

## Executed evidence

Logs and disposable input files are under
`/tmp/memweft-implementation/auth/import-order/`.

- `before.json` records the incorrect actual planner order for eight mixed
  punctuation, case and Unicode IDs. The added production-planner regression
  failed before the fix (`unit-before.log`). Afterward `npm --prefix auth-server
  test` passed all 49 tests (`auth-tests.log`). Reversing export input order
  preserves the same canonical digest.
- The unchanged `auth-server/Dockerfile` built
  `memweft-auth-import-order:20260904` for Linux AMD64, OCI index
  `sha256:568d7da6939a27db5dee63b4fcb4e5b16d01dde9e72f96b8e6fe95feace2784f`.
  Labels identify the local uncommitted candidate based on `69929dcc98`; this is
  not a full immutable release. The actual image importer SHA-256 matched the
  checked-out source. No source mounts were used; only private synthetic import
  inputs were mounted.
- `live.py` created two fresh temporary databases in the owned PG16 fixture.
  The old AMD64 image reproduced the mixed-UID apply failure and left all four
  user/account/session/import-ledger tables empty after rollback.
- The new image's actual CLI validate/apply/verify/reapply passed for all eight
  exact UIDs. PostgreSQL returned the expected byte order; counts were eight
  users, eight accounts, zero sessions and one import receipt.
- Reapplying a modified source was rejected. The original counts and receipt
  hashes remained unchanged.
- A separate database was imported with the old image using an order-compatible
  legacy export. The new image verified and reapplied that existing receipt
  without changing either hash, exercising the previous successful principal
  shape as well as the formerly rejected export.
- Both temporary databases were removed; the original Auth fixture was retained.
  `live.log` records all results. This is an actual operator migration CLI path,
  not a browser or native-client qualification.

No upstream file, dependency lock, live account or production database was
modified. The new failure-class definition records the observed canonical-order
boundary; it does not transition any existing registry entry. The exact candidate
gate commands/results are recorded in the commit message and adjacent gate logs.
