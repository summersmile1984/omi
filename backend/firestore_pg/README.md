# firestore_pg — PostgreSQL shim for `google.cloud.firestore`

A drop-in replacement for the Google Cloud Firestore client that backs the Omi
backend's `database/*.py` modules against PostgreSQL instead of Firestore. The
repo can run **zero-change** business code on a local Postgres; the same code
still runs against real Firestore when the shim is not installed.

```
database/*.py (unchanged)  ──►  google.cloud.firestore (facade)
                                        │
                    firestore_pg: SQLAlchemy 2.0 → PostgreSQL (JSONB)
```

## Why

`database/*.py` (88 modules) imports `google.cloud.firestore` at module scope.
`firestore_pg.compat.install()` overwrites `sys.modules["google.cloud.firestore"]`
with a facade whose objects (`Client`, `CollectionReference`, `DocumentReference`,
`Query`, `DocumentSnapshot`, `Transaction`, `FieldFilter`, `ArrayUnion`,
`ArrayRemove`, `Increment`, `DELETE_FIELD`, `SERVER_TIMESTAMP`,
`@firestore.transactional`) execute real SQL against PostgreSQL. Unknown
attributes forward to the real SDK, so constants and helpers that the shim does
not reimplement still resolve.

## Installation

```bash
# backend/ — Python 3.11 (required, matches the Docker base)
./scripts/sync-python-deps.sh
```

The shim is a plain package inside the repo (`firestore_pg/`); it needs
`sqlalchemy` and `psycopg[binary]` (already in the backend lock).

## Running

Set `FIRESTORE_PG_DSN` to a SQLAlchemy PostgreSQL URL. When it is set,
`database/__init__.py` calls `firestore_pg.compat.install()` before any
`database.*` module imports the SDK, so business code resolves to the shim.

```bash
export FIRESTORE_PG_DSN="postgresql+psycopg://omi:omi-dev-password@localhost:5434/omi"
export FIRESTORE_EMULATOR_HOST=localhost:8080        # Auth/storage still emulated
export FIREBASE_AUTH_EMULATOR_HOST=localhost:9099
export STORAGE_EMULATOR_HOST=localhost:9199
export FIREBASE_PROJECT_ID=demo-omi-local
export ENCRYPTION_SECRET='...'                        # 32-byte base64 dev secret
uvicorn main:app --host 127.0.0.1 --port 8100
```

Schema is owned by the forward-only migration CLI; runtime clients never create
tables or indexes. Run migration and its read-only admission check before any
backend or worker process:

```bash
python scripts/firestore_pg_migrate.py migrate
python scripts/firestore_pg_migrate.py check
```

The migration uses a PostgreSQL advisory transaction lock and records every
applied version in `firestore_pg_schema_migrations`. Collection tables are
registered in `firestore_pg_collections`; an unregistered collection fails
closed with an instruction to run the migration/import owner. Tables remain
keyed off `resolve_collection`: `users` → table `users` (uid column empty),
`users/{uid}/conversations` → table `conversations` with
`uid = users/{uid}`. The legacy column name `uid` stores the complete
parent-document path: a photo at
`users/u/conversations/c1/photos/p` is stored in table `photos` under namespace
`users/u/conversations/c1`. This prevents sibling conversations and users from
sharing nested documents and lets collection-group snapshots reconstruct their
authoritative Firestore paths.

### Dev stack (three emulators + Postgres)

`dev/docker-compose.dev.yml` runs the Firestore/Auth/Storage emulators plus a
Postgres container (`127.0.0.1:5434`). Bring it up, point `FIRESTORE_PG_DSN` at
it, and start the backend.

## Behavior notes

- **Writes autocommit** outside transactions (`engine.begin()`); a bare
  `engine.connect()` silently rolls back on close in SQLAlchemy 2.0.
- **`update` on a missing document raises** `NotFound`.
- **`create` on an existing document raises** `AlreadyExists` (Firestore
  semantics), using one `INSERT ... ON CONFLICT` statement so concurrent
  creators have exactly one winner.
- **Ordering** excludes documents missing the ordered field, uses native JSONB
  numeric ordering, and appends the Firestore document-name tie-breaker.
  Timestamps are stored as canonical UTC RFC3339 with exactly nine fractional
  digits. Firestore's microsecond truncation is preserved (including dates
  before 1970), so the last three digits are always `000` and lexical order is
  chronological order. Direct `DatetimeWithNanoseconds` writes also match the
  Google SDK's toward-zero negative-epoch `timestamp_pb()` conversion; the
  migration importer separately preserves the calendar value of an already
  stored source snapshot so copying data cannot apply that conversion twice.
- **Write batches are atomic**: every queued set/update/delete/create is
  committed or rolled back on one PostgreSQL connection.
- **CAS preconditions are real**: every row has an authoritative `updated_at`
  and monotonic `version`; `Client.write_option(last_update_time=...)` and SDK
  `LastUpdateOption` reject stale batch, update, and delete operations.
- **Account deletion discovers collections from stored rows**, rather than a
  hand-maintained collection list, so newly introduced user collections are
  included automatically.
- **Dotted field paths** (`update({"a.b": v})`, `DELETE_FIELD`) are honored on
  nested JSONB; an emptied parent object is kept as `{}` like Firestore.
- **Transform fields** (`Increment`, `ArrayUnion`, `ArrayRemove`,
  `DELETE_FIELD`) are applied read-modify-write on the transaction connection.
  Array transforms use Firestore's recursive numeric equality (including
  canonical NaN equality), rather than Python's `in` semantics.
- **Typed Firestore values round-trip losslessly**: timestamps, bytes,
  every IEEE-754 double, `GeoPoint`, document references, strings containing
  NUL, maps with NUL keys, and non-finite floats use collision-free tagged
  JSONB. Double bits preserve `-0.0` and large finite values without JSONB
  coercing them to integers; NaNs are canonicalized like Firestore. Ordinary
  ISO-8601 strings remain strings; only values written as timestamps decode as
  timestamps. Internal read-modify-write cycles preserve already-stored
  pre-1970 timestamps instead of reapplying the SDK's negative-epoch wire
  conversion.
- **Document roots are encoded separately from map values**. Firestore-reserved
  field names matching `__.*__` fail closed, so the tagged-value envelope
  cannot collide with a valid root document.
- **Numeric queries interleave integers and doubles** with an exact PostgreSQL
  `numeric` projection of each IEEE-754 value. Equality, `in`/`not-in`, array
  membership, range, ordering, and cursors therefore treat `1`/`1.0` and
  `-0.0`/`0.0` like Firestore while reads retain their original Python types.
- **Query field paths fail closed before SQL generation** unless they are
  dot-separated Firestore simple identifiers. The complete `__name__` sentinel
  is supported; quoted/escaped/reserved/special field names raise
  `UnsupportedFirestoreQuery` until an equivalent parser is implemented.
- **`@firestore.transactional`** uses the same PG connection for the whole
  `SERIALIZABLE` transaction; serialization conflicts raise
  `google.api_core.exceptions.Aborted`, retry up to the transaction's
  `_max_attempts` (default 5), and terminate with the SDK-compatible
  `ValueError` chained from `Aborted` when attempts are exhausted.

## Known limitations

- `on_snapshot` (realtime listeners) is **not** implemented — no business module
  uses it.
- Collection-group queries and the repository's registered composite indexes
  are supported. PostgreSQL still does not reproduce Firestore's index-admission
  errors; an unregistered compound query can execute as an expression scan.
- Arrays and maps store losslessly, and the shim supports the serving
  `array-contains` and `array-contains-any` surfaces. Range filters, ordering,
  and cursors over whole array/map values fail closed rather than inherit
  PostgreSQL JSONB ordering. Exact `==`/`!=`, `in`/`not-in`, and array
  membership whose query candidate is itself an array/map also fail closed;
  direct JSONB equality cannot reproduce Firestore's recursive integer/double
  equivalence.
- Exact equality and membership queries support bytes, `GeoPoint`, document
  references, and non-finite floats. NaN `in`/`not-in` follows the emulator's
  distinct membership behavior; NaN array-membership filters fail closed like
  the Google SDK. Range filters, ordering, and cursors on those value families
  fail closed with `UnsupportedFirestoreQuery`; the shim does not silently
  substitute PostgreSQL JSONB ordering for Firestore's type order. Nested
  queries through a map containing a NUL key also fail closed. Document-name
  (`__name__`) reference cursors remain supported.
- No Firestore emulator in this path: the shim talks to Postgres directly.

## Verification

- **Shadow diff (regression lane)** — `dev/shadow-diff.sh` (or `make dev-shadow-diff`)
  runs the same scenario sequence against the real SDK (emulator) and the shim
  (PG) and diffs normalized JSON; exits 1 on mismatch. 29 scenarios cover CRUD,
  merge, update, delete, `==`/comparison/`in` queries, order+limit, dotted-path
  compound queries, dotted order_by, ArrayUnion/Remove, Increment,
  DELETE_FIELD, nested collections, transactional counters, and
  SERVER_TIMESTAMP, sibling nested-parent isolation, collection-group paths,
  atomic batch failure, numeric/missing-field ordering, snapshot/mapping
  cursors, update-time CAS, transaction create, explicit-ID add, nested
  collection-group isolation, and same-microsecond timestamp order/range/cursor
  parity (`123456000` versus `123456789` nanosecond inputs). Requires the dev
  stack (`dev/dev-up.sh --no-backend`). Current result: **29/29 match**.
- **Transaction and production parity semantics** —
  `firestore_pg/tests/test_transaction_semantics.py` (integration; skipped
  without `FIRESTORE_PG_DSN`): commit visibility, conflict retry without lost
  updates, complete nested namespaces, collection-group paths, atomic batches,
  snapshot/dict cursors, numeric ordering, missing/null query behavior, CAS,
  concurrent create, a live concurrent write-skew probe under `SERIALIZABLE`,
  recursive collection discovery, typed unsupported-order failures, and all
  compatibility methods used by backend business code.
- **Composite indexes** — `firestore_pg/tests/test_composite_indexes.py`
  (integration; skipped without `FIRESTORE_PG_DSN`): registry tables exist,
  composite indexes created from `firestore_index_registry`, dotted-path
  expressions use nested `#>>`, index creation idempotent.

## Firestore import and production gate

The first shim schema stored only a bare user ID and collapsed deeper
subcollections into underscore-joined table names. Migration version 1
losslessly upgrades first-level namespaces (`u` → `users/u`) and adds
`updated_at`/`version`, but it cannot infer a deleted parent ID from already
collapsed nested rows. An existing revision-1 database must therefore be
rebuilt from the Firestore source before production cutover; do not point a
production deployment at that volume.

The import CLI recursively uses Firestore `list_documents(show_missing=True)`
and `DocumentReference.collections()`, so subcollections below missing parents
retain their real full paths and collection-group identity. Unknown collection
IDs are dynamically provisioned through the same locked schema owner. Schema
version 1's 27 raw PostgreSQL collection-table identifiers are a frozen
migration artifact; every other valid Firestore collection ID maps to a stable
full SHA-256 table identifier. Schema version 2 explicitly provisions the
production backend's static inventory, including account-deletion and
conversation-finalization control collections, while retaining those hashed
mappings. A unit inventory scan rejects new literal or named collection
references until a new explicit schema version owns them; future additions
cannot mutate either frozen version's mapping.

```bash
export FIRESTORE_PG_DSN='postgresql+psycopg://...'
python scripts/firestore_pg_migrate.py import \
  --source-project source-project-id \
  --source-database '(default)' \
  --source-endpoint https://firestore.googleapis.com \
  --source-credentials /secure/firestore-reader.json \
  --checkpoint /secure/change-record/firestore-import.json \
  --freeze-lease /secure/change-record/source-freeze.json
```

`--source-endpoint` is passed to the Firestore SDK as the actual API target,
then recorded and checked in the checkpoint. It may identify the managed
Firestore authority or an operator-owned Firestore-compatible endpoint; the
importer never connects to a default authority and merely compares it after
the fact.

The checkpoint and adjacent JSONL document manifest are mode `0600`. The
checkpoint binds every resume to the source project, database, resolved API
endpoint, and emulator authority (when present); any authority change fails
closed. Preserve both to resume after interruption; they contain customer data
and must be kept in encrypted operator-controlled storage, then securely
removed per policy. Source writes must be paused by external change control and
proved with the mode-0600 HMAC freeze lease before the import begins.
Starting without a checkpoint refuses a non-empty target. Completion rescans
the live source and independently enumerates all registered PG tables; source
snapshot, live source, and target must have identical document counts and
canonical content hashes. Source writes must therefore be quiesced for the
final pass. Any unsupported value, source drift, missing/edited checkpoint, or
count/hash mismatch exits nonzero and does not authorize cutover.

For a fresh/re-imported database, PostgreSQL behavior is covered by the contract
suite above. `deploy/self-host/migration-cutover-gate.sh` executes migration
twice, checks the ledger, imports a real missing-parent nested emulator fixture,
reconciles count/content hashes, then runs the live PG suite and 29-scenario
emulator shadow diff. Production enablement still requires the repository-wide
deployment gate, backups, live source freeze, and rollback—not merely setting
`FIRESTORE_PG_DSN` on one process.
