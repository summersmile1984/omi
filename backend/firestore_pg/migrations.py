"""Forward-only schema ownership for the Firestore PostgreSQL shim.

Runtime clients are deliberately read-only with respect to PostgreSQL schema.
Only this module may create or upgrade shim tables, and every change runs while
holding one PostgreSQL advisory transaction lock.  Dynamic collection IDs found
during a Firestore import are provisioned through the same owner and recorded in
``firestore_pg_collections`` so runtime discovery never scrapes unrelated tables.
"""

from __future__ import annotations

import base64
import hashlib
import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from database.firestore_index_registry import INDEX_REQUIREMENTS

from .engine import KNOWN_COLLECTIONS, create_composite_indexes, get_engine
from .sql import build_ddl, resolve_collection

LATEST_SCHEMA_VERSION = 2
MIGRATION_LOCK_ID = 7_362_737_641_104_927_311
MIGRATION_TABLE = 'firestore_pg_schema_migrations'
COLLECTION_TABLE = 'firestore_pg_collections'
REQUIRED_DOCUMENT_COLUMNS = {'uid', 'doc_id', 'data', 'created_at', 'updated_at', 'version'}
_SAFE_TABLE = re.compile(r'^[a-z_][a-z0-9_]{0,62}$')

# Schema v1's raw physical identifiers are immutable migration data.  Never
# derive this set from the current source inventory: an upstream collection
# added later must not change a formerly-dynamic ID from its SHA-256 table to a
# raw table name.  Adding a statically-known collection requires a new schema
# migration/version which explicitly chooses and records its mapping.
LEGACY_RAW_COLLECTION_IDS_V1 = frozenset(
    {
        'action_items',
        'candidate_integration_outbox',
        'candidates',
        'chat_first_deferrals',
        'chat_sessions',
        'conversation_finalization_jobs',
        'conversations',
        'events',
        'fair_use_state',
        'fcm_tokens',
        'files',
        'folders',
        'goals',
        'llm_usage',
        'memories',
        'memory_items',
        'memory_operations',
        'memory_outbox',
        'memory_review_queue',
        'messages',
        'notifications',
        'screen_activity',
        'staged_tasks',
        'task_attention_overrides',
        'tasks',
        'users',
        'workstreams',
    }
)

# Schema v2 freezes the exhaustive production inventory added after v1.  These
# IDs intentionally keep the hashed mapping they had while they were dynamic;
# adding another ID requires schema v3 rather than mutating this snapshot.
STATIC_HASHED_COLLECTION_IDS_V2 = frozenset(
    {
        'account_cutover',
        'account_deletion_receipts',
        'account_deletions',
        'agentVmMigrations',
        'analytics',
        'analytics_markers',
        'announcements',
        'api_keys',
        'app_review_config',
        'artifact_heads',
        'artifact_refs',
        'candidate_idempotency_aliases',
        'candidate_pending_claims',
        'candidate_resolution_claims',
        'canonical_memory_atoms',
        'canonical_memory_dreaming_state',
        'canonical_memory_maintenance_registry',
        'chat_first_proactive_intents',
        'chat_first_proactive_state',
        'chat_quota_events',
        'client_devices',
        'continuation_checkpoints',
        'conversation_finalization_projection_shards',
        'conversation_recovery_state',
        'daily_summaries',
        'deepgram_streaming',
        'desktop_beta_admission',
        'desktop_beta_breakglass_audits',
        'desktop_preview_manifests',
        'desktop_preview_pointers',
        'desktop_release_manifests',
        'desktop_releases',
        'desktop_update_channels',
        'desktop_update_policy',
        'dev_api_keys',
        'dismissed_announcements',
        'fair_use_events',
        'fal_whisperx',
        'goal_history',
        'hourly_usage',
        'import_jobs',
        'integrations',
        'knowledge_edges',
        'knowledge_nodes',
        'llm_gateway_attempts',
        'llm_runtime_controls',
        'mcp_api_keys',
        'mcp_oauth_access_tokens',
        'mcp_oauth_authorization_codes',
        'mcp_oauth_clients',
        'mcp_oauth_grants',
        'mcp_oauth_refresh_tokens',
        'meetings',
        'memory_commits',
        'memory_control',
        'memory_corrections',
        'memory_evidence',
        'memory_graph_assertions',
        'memory_historical_overrides',
        'memory_import_artifacts',
        'memory_import_candidates',
        'memory_import_runs',
        'memory_legacy_fallback',
        'memory_lineage',
        'memory_runs',
        'memory_source_replacements',
        'memory_state',
        'non_active_memory_routes',
        'pending_verifications',
        'people',
        'phone_call_config',
        'phone_numbers',
        'photos',
        'plugins',
        'plugins_data',
        'projection_repairs',
        'realtime_sessions',
        'recording_sessions',
        'reviews',
        'short_term',
        'short_term_lifecycle_transitions',
        'soniox_streaming',
        'speechmatics_streaming',
        'sync_content_ledger',
        'task_context_snapshots',
        'task_feedback',
        'task_integrations',
        'task_intelligence_control',
        'task_interventions',
        'task_open_loop_snapshots',
        'task_outcomes',
        'task_recommendation_decisions',
        'task_recommendation_projections',
        'task_recurrence_inbox',
        'task_snapshot_receipts',
        'testers',
        'topics',
        'trends',
        'usage_history',
        'work_intent_receipts',
        'workflow_mutation_receipts',
        'wrapped',
        'x_connector_users',
        'x_posts',
    }
)


class SchemaNotCurrent(RuntimeError):
    """The database has not been admitted by the explicit migration owner."""


@dataclass(frozen=True)
class SchemaStatus:
    current_version: int
    latest_version: int
    collections: tuple[str, ...]


_verified_tables: set[str] = set()
_verified_tables_lock = threading.Lock()


def _declared_known_collections() -> set[str]:
    names = set(KNOWN_COLLECTIONS)
    names.update(req.collection_group for req in INDEX_REQUIREMENTS)
    return names


def _assert_known_inventory_versioned() -> None:
    versioned = LEGACY_RAW_COLLECTION_IDS_V1 | STATIC_HASHED_COLLECTION_IDS_V2
    declared = _declared_known_collections()
    added = declared - versioned
    if added:
        raise SchemaNotCurrent(
            'new statically-known collections require a new firestore_pg schema migration/version: '
            + ', '.join(sorted(added))
        )
    stale = versioned - declared
    if stale:
        raise SchemaNotCurrent(
            'schema-owned collections are absent from the production inventory: ' + ', '.join(sorted(stale))
        )


def known_collections() -> tuple[str, ...]:
    """Return every frozen statically-known production collection ID."""
    _assert_known_inventory_versioned()
    return tuple(sorted(LEGACY_RAW_COLLECTION_IDS_V1 | STATIC_HASHED_COLLECTION_IDS_V2))


def validate_collection_id(collection_id: Any) -> str:
    """Validate a Firestore collection-ID segment without narrowing its charset."""
    if not isinstance(collection_id, str) or not collection_id or '/' in collection_id or '\x00' in collection_id:
        raise ValueError(f'invalid Firestore collection ID {collection_id!r}')
    return collection_id


def collection_table_name(collection_id: str) -> str:
    """Map dynamic IDs with SHA-256; only schema-v1 IDs retain raw names."""
    collection_id = validate_collection_id(collection_id)
    if collection_id in LEGACY_RAW_COLLECTION_IDS_V1:
        return collection_id
    digest = (
        base64.b32encode(hashlib.sha256(collection_id.encode('utf-8')).digest()).decode('ascii').rstrip('=').lower()
    )
    table = f'f_{digest}'
    if not _SAFE_TABLE.fullmatch(table):  # pragma: no cover - construction invariant
        raise AssertionError(f'unsafe generated collection table name: {table!r}')
    return table


def _bootstrap_ledger(conn: Connection) -> None:
    conn.execute(text('SELECT pg_advisory_xact_lock(:lock_id)'), {'lock_id': MIGRATION_LOCK_ID})
    conn.execute(
        text(
            f'CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} ('
            'version INTEGER PRIMARY KEY, name TEXT NOT NULL, '
            'applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())'
        )
    )
    conn.execute(
        text(
            f'CREATE TABLE IF NOT EXISTS {COLLECTION_TABLE} ('
            'collection_id TEXT PRIMARY KEY, table_name TEXT NOT NULL UNIQUE, '
            'registered_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())'
        )
    )


def _legacy_collection_tables(conn: Connection) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name NOT IN (:migrations, :collections) "
            "AND column_name IN ('uid', 'doc_id', 'data') "
            'GROUP BY table_name HAVING count(DISTINCT column_name) = 3'
        ),
        {'migrations': MIGRATION_TABLE, 'collections': COLLECTION_TABLE},
    ).fetchall()
    return {str(row[0]) for row in rows}


def _legacy_tables_with_rows(conn: Connection, tables: Iterable[str]) -> tuple[str, ...]:
    populated = []
    for table in sorted(tables):
        if not _SAFE_TABLE.fullmatch(table):
            raise SchemaNotCurrent(f'unsafe legacy PostgreSQL table identifier requires manual migration: {table!r}')
        if conn.execute(text(f'SELECT 1 FROM {table} LIMIT 1')).fetchone() is not None:
            populated.append(table)
    return tuple(populated)


def _register_collection(conn: Connection, collection_id: str) -> None:
    logical_id, parent = resolve_collection(collection_id)
    if parent:
        raise ValueError(f'expected a collection ID, got path {collection_id!r}')
    table = collection_table_name(logical_id)
    conn.execute(text(build_ddl(table)))
    conn.execute(
        text(
            f'INSERT INTO {COLLECTION_TABLE} (collection_id, table_name) '
            'VALUES (:collection_id, :table_name) '
            'ON CONFLICT (collection_id) DO UPDATE SET table_name = EXCLUDED.table_name'
        ),
        {'collection_id': collection_id, 'table_name': table},
    )


def _apply_v1(conn: Connection) -> None:
    legacy = _legacy_collection_tables(conn)
    populated_legacy = _legacy_tables_with_rows(conn, legacy)
    if populated_legacy:
        raise SchemaNotCurrent(
            'legacy firestore_pg tables contain rows with ambiguous parent paths/value encoding; '
            'migrate into a fresh target with the authoritative Firestore importer: ' + ', '.join(populated_legacy)
        )
    unmanaged_legacy = legacy - set(LEGACY_RAW_COLLECTION_IDS_V1)
    if unmanaged_legacy:
        raise SchemaNotCurrent(
            'legacy firestore_pg has unknown direct-table mappings; migrate into a fresh target: '
            + ', '.join(sorted(unmanaged_legacy))
        )
    for collection_id in sorted(LEGACY_RAW_COLLECTION_IDS_V1):
        _register_collection(conn, collection_id)

    create_composite_indexes(conn, collection_table_name)


def _apply_v2(conn: Connection) -> None:
    for collection_id in sorted(STATIC_HASHED_COLLECTION_IDS_V2):
        _register_collection(conn, collection_id)
    create_composite_indexes(conn, collection_table_name)


def migrate(engine: Optional[Engine] = None) -> SchemaStatus:
    """Apply every unapplied forward migration under one advisory lock."""
    _assert_known_inventory_versioned()
    engine = engine or get_engine()
    with engine.begin() as conn:
        _bootstrap_ledger(conn)
        applied = {
            int(row[0])
            for row in conn.execute(text(f'SELECT version FROM {MIGRATION_TABLE} ORDER BY version')).fetchall()
        }
        unknown = sorted(version for version in applied if version > LATEST_SCHEMA_VERSION)
        if unknown:
            raise SchemaNotCurrent(f'database schema is newer than this runtime: versions={unknown}')
        if 1 not in applied:
            _apply_v1(conn)
            conn.execute(
                text(f'INSERT INTO {MIGRATION_TABLE} (version, name) VALUES (1, :name)'),
                {'name': 'full_parent_paths_versions_collection_registry'},
            )
        if 2 not in applied:
            _apply_v2(conn)
            conn.execute(
                text(f'INSERT INTO {MIGRATION_TABLE} (version, name) VALUES (2, :name)'),
                {'name': 'production_static_collection_inventory'},
            )
    return check_schema(engine)


def provision_collections(collection_ids: Iterable[str], engine: Optional[Engine] = None) -> tuple[str, ...]:
    """Explicitly provision dynamically discovered import collections."""
    _assert_known_inventory_versioned()
    engine = engine or get_engine()
    normalized = tuple(sorted({validate_collection_id(name) for name in collection_ids}))
    with engine.begin() as conn:
        _bootstrap_ledger(conn)
        version = conn.execute(text(f'SELECT max(version) FROM {MIGRATION_TABLE}')).scalar()
        if int(version or 0) != LATEST_SCHEMA_VERSION:
            raise SchemaNotCurrent(
                'run `python scripts/firestore_pg_migrate.py migrate` before provisioning collections'
            )
        registered_tables = {
            str(row[0]) for row in conn.execute(text(f'SELECT table_name FROM {COLLECTION_TABLE}')).fetchall()
        }
        unmanaged_legacy = _legacy_collection_tables(conn) - registered_tables
        if unmanaged_legacy:
            raise SchemaNotCurrent(
                'unmanaged legacy collection tables require authoritative import into a fresh target: '
                + ', '.join(sorted(unmanaged_legacy))
            )
        for collection_id in normalized:
            _register_collection(conn, collection_id)
    return normalized


def _table_columns(conn: Connection, table: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            text(
                'SELECT column_name FROM information_schema.columns '
                'WHERE table_schema = current_schema() AND table_name = :table'
            ),
            {'table': table},
        ).fetchall()
    }


def check_schema(engine: Optional[Engine] = None) -> SchemaStatus:
    """Read-only schema admission check used by runtime processes and gates."""
    _assert_known_inventory_versioned()
    engine = engine or get_engine()
    with engine.connect() as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
            ).fetchall()
        }
        missing_control = {MIGRATION_TABLE, COLLECTION_TABLE} - tables
        if missing_control:
            raise SchemaNotCurrent('firestore_pg schema is not migrated; missing ' + ', '.join(sorted(missing_control)))
        versions = [
            int(row[0])
            for row in conn.execute(text(f'SELECT version FROM {MIGRATION_TABLE} ORDER BY version')).fetchall()
        ]
        if versions != list(range(1, LATEST_SCHEMA_VERSION + 1)):
            raise SchemaNotCurrent(
                f'firestore_pg schema versions {versions!r} do not match expected ' f'1..{LATEST_SCHEMA_VERSION}'
            )
        registered = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                text(f'SELECT collection_id, table_name FROM {COLLECTION_TABLE} ORDER BY collection_id')
            ).fetchall()
        }
        missing_known = set(known_collections()) - set(registered)
        if missing_known:
            raise SchemaNotCurrent('known collections are not migrated: ' + ', '.join(sorted(missing_known)))
        for collection_id, table in registered.items():
            if table != collection_table_name(collection_id):
                raise SchemaNotCurrent(f'invalid collection registry mapping: {collection_id!r} -> {table!r}')
            missing_columns = REQUIRED_DOCUMENT_COLUMNS - _table_columns(conn, table)
            if missing_columns:
                raise SchemaNotCurrent(
                    f'collection {collection_id!r} is missing columns: {", ".join(sorted(missing_columns))}'
                )

        unmanaged = _legacy_collection_tables(conn) - set(registered.values())
        if unmanaged:
            raise SchemaNotCurrent(
                'unmanaged legacy collection tables require migration: ' + ', '.join(sorted(unmanaged))
            )
    return SchemaStatus(LATEST_SCHEMA_VERSION, LATEST_SCHEMA_VERSION, tuple(sorted(registered)))


def require_collection(collection_id: str, engine: Optional[Engine] = None) -> str:
    """Read-only assertion that a collection was explicitly provisioned."""
    collection_id = validate_collection_id(collection_id)
    engine = engine or get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(f'SELECT table_name FROM {COLLECTION_TABLE} WHERE collection_id = :collection_id'),
            {'collection_id': collection_id},
        ).fetchone()
        if row is None:
            raise SchemaNotCurrent(
                f'collection {collection_id!r} is not provisioned; run the migration/import owner before serving'
            )
        table = str(row[0])
        if table != collection_table_name(collection_id):
            raise SchemaNotCurrent(f'invalid collection registry mapping: {collection_id!r} -> {table!r}')
        missing = REQUIRED_DOCUMENT_COLUMNS - _table_columns(conn, table)
        if missing:
            raise SchemaNotCurrent(f'collection {collection_id!r} has stale schema: {sorted(missing)}')
    return table


def require_table_name(table_name: str, engine: Optional[Engine] = None) -> str:
    """Assert that an already-resolved physical table is migration-owned."""
    if not _SAFE_TABLE.fullmatch(table_name):
        raise SchemaNotCurrent(f'invalid physical collection table name: {table_name!r}')
    engine = engine or get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(f'SELECT collection_id FROM {COLLECTION_TABLE} WHERE table_name = :table_name'),
            {'table_name': table_name},
        ).fetchone()
        logical_id = str(row[0]) if row is not None else ''
        valid_mapping = row is not None and collection_table_name(logical_id) == table_name
        if not valid_mapping:
            raise SchemaNotCurrent(f'collection table {table_name!r} is not provisioned by the migration owner')
        missing = REQUIRED_DOCUMENT_COLUMNS - _table_columns(conn, table_name)
        if missing:
            raise SchemaNotCurrent(f'collection table {table_name!r} has stale schema: {sorted(missing)}')
    return table_name


def require_table(table_name: str, engine: Optional[Engine] = None) -> str:
    """Cached runtime admission check; it never creates or upgrades schema."""
    if engine is None and table_name in _verified_tables:
        return table_name
    require_table_name(table_name, engine)
    if engine is None:
        with _verified_tables_lock:
            _verified_tables.add(table_name)
    return table_name
