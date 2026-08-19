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
`conversations` with `uid = {uid}`, nested subcollections get underscore-joined
table names.

### Dev stack (three emulators + Postgres)

`dev/docker-compose.dev.yml` runs the Firestore/Auth/Storage emulators plus a
Postgres container (`127.0.0.1:5434`). Bring it up, point `FIRESTORE_PG_DSN` at
it, and start the backend.

## Behavior notes

- **Writes autocommit** outside transactions (`engine.begin()`); a bare
  `engine.connect()` silently rolls back on close in SQLAlchemy 2.0.
- **`update` on a missing document raises** `ValueError` (Firestore raises
  NotFound) — create the document first (e.g. the onboarding flow).
- **`create` on an existing document raises** `AlreadyExists` (Firestore
  semantics).
- **Ordering** sorts documents missing the sort field last, matching Firestore
  (Postgres would otherwise put NULLs first on DESC).
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
- Collection-group queries are supported; Firestore composite-index semantics
  are not replicated (single-field JSONB predicates only).
- Arrays store as JSONB; ordering/inequality on array elements is limited.
- No Firestore emulator in this path: the shim talks to Postgres directly.

## Verification

- **Shadow diff (regression lane)** — `dev/shadow-diff.sh` (or `make dev-shadow-diff`)
  runs the same scenario sequence against the real SDK (emulator) and the shim
  (PG) and diffs normalized JSON; exits 1 on mismatch. 16 scenarios cover CRUD,
  merge, update, delete, `==`/comparison/`in` queries, order+limit, dotted-path
  compound queries, dotted order_by, ArrayUnion/Remove, Increment,
  DELETE_FIELD, nested collections, transactional counters, and
  SERVER_TIMESTAMP. Requires the dev stack (`dev/dev-up.sh --no-backend`).
  Current result: **16/16 match**.
- **Transaction semantics** — `firestore_pg/tests/test_transaction_semantics.py`
  (integration; skipped without `FIRESTORE_PG_DSN`): commit visibility, conflict
  retry without lost updates, create/update guards, rollback atomicity.
- **Composite indexes** — `firestore_pg/tests/test_composite_indexes.py`
  (integration; skipped without `FIRESTORE_PG_DSN`): registry tables exist,
  composite indexes created from `firestore_index_registry`, dotted-path
  expressions use nested `#>>`, index creation idempotent.

## Migration

The shim is the **local/dev** path. Production Firestore remains authoritative.
To move business code onto Postgres for good, first run shadow-diff continuously
on the dev stack, then flip the deploy to set `FIRESTORE_PG_DSN` for a staged
subset. See `omi-shim-and-emulators.md` §4 for the three-phase migration plan.
