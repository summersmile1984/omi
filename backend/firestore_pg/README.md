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

Tables are created on demand (`ensure_table`), keyed off `resolve_collection`:
`users` → table `users` (uid column empty), `users/{uid}/conversations` → table
`conversations` with `uid = users/{uid}`. The legacy column name `uid` now
stores the complete parent-document path: a photo at
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
- **ISO-8601 strings** read back as `datetime` where the real SDK returns
  datetimes (e.g. `is_byok_active`).
- **`@firestore.transactional`** uses the same PG connection for the whole
  transaction; serialization conflicts raise `google.api_core.exceptions.Aborted`
  (retried by the standard retry helper).

## Known limitations

- `on_snapshot` (realtime listeners) is **not** implemented — no business module
  uses it.
- Collection-group queries and the repository's registered composite indexes
  are supported. PostgreSQL still does not reproduce Firestore's index-admission
  errors; an unregistered compound query can execute as an expression scan.
- Arrays store as JSONB; Firestore does not define ordering/inequality on array
  elements, and the shim supports the serving `array-contains` and
  `array-contains-any` surfaces only.
- No Firestore emulator in this path: the shim talks to Postgres directly.

## Verification

- **Shadow diff (regression lane)** — `dev/shadow-diff.sh` (or `make dev-shadow-diff`)
  runs the same scenario sequence against the real SDK (emulator) and the shim
  (PG) and diffs normalized JSON; exits 1 on mismatch. 27 scenarios cover CRUD,
  merge, update, delete, `==`/comparison/`in` queries, order+limit, dotted-path
  compound queries, dotted order_by, ArrayUnion/Remove, Increment,
  DELETE_FIELD, nested collections, transactional counters, and
  SERVER_TIMESTAMP, sibling nested-parent isolation, collection-group paths,
  atomic batch failure, numeric/missing-field ordering, snapshot/mapping
  cursors, update-time CAS, transaction create, and explicit-ID add. Requires
  the dev stack (`dev/dev-up.sh --no-backend`). Current result: **27/27 match**.
- **Transaction and production parity semantics** —
  `firestore_pg/tests/test_transaction_semantics.py` (integration; skipped
  without `FIRESTORE_PG_DSN`): commit visibility, conflict retry without lost
  updates, complete nested namespaces, collection-group paths, atomic batches,
  snapshot/dict cursors, numeric ordering, missing/null query behavior, CAS,
  concurrent create, recursive collection discovery, and all compatibility
  methods used by backend business code.
- **Composite indexes** — `firestore_pg/tests/test_composite_indexes.py`
  (integration; skipped without `FIRESTORE_PG_DSN`): registry tables exist,
  composite indexes created from `firestore_index_registry`, dotted-path
  expressions use nested `#>>`, index creation idempotent.

## Migration and production gate

The first shim schema stored only a bare user ID and collapsed deeper
subcollections into underscore-joined table names. Startup losslessly upgrades
first-level namespaces (`u` → `users/u`) and adds `updated_at`/`version`, but it
cannot infer a deleted parent ID from already-collapsed nested rows. An existing
revision-1 database must therefore be rebuilt from the Firestore shadow source
before production cutover; do not point a production deployment at that volume.

For a fresh/re-imported database, PostgreSQL behavior is now covered by the
contract suite above. Production enablement still requires the repository-wide
deployment gate (backups, migration job, live shadow parity, and rollback), not
merely setting `FIRESTORE_PG_DSN` on one process.
