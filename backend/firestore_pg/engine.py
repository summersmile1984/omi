"""Connection management for firestore_pg.

Uses SQLAlchemy 2.0 (already a backend dependency) with psycopg 3 for sync
transactions. Connection parameters come from the standard PG* env vars, or
``FIRESTORE_PG_DSN`` as an override.

The design keeps a single engine shared by all shim clients; transactions are
scoped per ``@firestore.transactional`` call via a thread-local connection.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url

from database.firestore_index_registry import INDEX_REQUIREMENTS

from .field_path import parse_field_path

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                dsn = os.environ.get("FIRESTORE_PG_DSN") or _dsn_from_env()
                _engine = create_engine(dsn, pool_pre_ping=True)
                logger.info("firestore_pg: engine created for %s", _dsn_host(dsn))
    return _engine


def _dsn_from_env() -> str:
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", "omi")
    password = os.environ.get("PGPASSWORD", "")
    db = os.environ.get("PGDATABASE", "omi")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


def _dsn_host(dsn: str) -> str:
    try:
        url = make_url(dsn)
        host = url.host or '<local-socket>'
        port = f':{url.port}' if url.port is not None else ''
        database = f'/{url.database}' if url.database else ''
        return f'{host}{port}{database}'
    except Exception:  # pragma: no cover - invalid URLs fail at create_engine
        return '<invalid-postgresql-url>'


# Production collection IDs referenced statically by backend/database,
# backend/services, routers, and production utils.  This is deliberately an
# explicit inventory: runtime code never creates schema, and migrations assert
# that every entry is owned by a frozen schema version before serving.
KNOWN_COLLECTIONS = frozenset(
    {
        'account_cutover',
        'account_deletion_receipts',
        'account_deletions',
        'action_items',
        'agentVmMigrations',
        'analytics',
        'analytics_markers',
        'announcements',
        'api_keys',
        'app_review_config',
        'artifact_heads',
        'artifact_refs',
        'candidate_idempotency_aliases',
        'candidate_integration_outbox',
        'candidate_pending_claims',
        'candidate_resolution_claims',
        'candidates',
        'canonical_memory_atoms',
        'canonical_memory_dreaming_state',
        'canonical_memory_maintenance_registry',
        'chat_first_deferrals',
        'chat_first_proactive_intents',
        'chat_first_proactive_state',
        'chat_quota_events',
        'chat_sessions',
        'client_devices',
        'continuation_checkpoints',
        'conversation_finalization_jobs',
        'conversation_finalization_projection_shards',
        'conversation_recovery_state',
        'conversations',
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
        'events',
        'fair_use_events',
        'fair_use_state',
        'fal_whisperx',
        'fcm_tokens',
        'files',
        'folders',
        'goal_history',
        'goals',
        'hourly_usage',
        'import_jobs',
        'integrations',
        'knowledge_edges',
        'knowledge_nodes',
        'llm_gateway_attempts',
        'llm_runtime_controls',
        'llm_usage',
        'mcp_api_keys',
        'mcp_oauth_access_tokens',
        'mcp_oauth_authorization_codes',
        'mcp_oauth_clients',
        'mcp_oauth_grants',
        'mcp_oauth_refresh_tokens',
        'meetings',
        'memories',
        'memory_commits',
        'memory_control',
        'memory_corrections',
        'memory_evidence',
        'memory_graph_assertions',
        'memory_historical_overrides',
        'memory_import_artifacts',
        'memory_import_candidates',
        'memory_import_runs',
        'memory_items',
        'memory_legacy_fallback',
        'memory_lineage',
        'memory_operations',
        'memory_outbox',
        'memory_review_queue',
        'memory_runs',
        'memory_source_replacements',
        'memory_state',
        'messages',
        'non_active_memory_routes',
        'notifications',
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
        'screen_activity',
        'short_term',
        'short_term_lifecycle_transitions',
        'soniox_streaming',
        'speechmatics_streaming',
        'staged_tasks',
        'sync_content_ledger',
        'task_attention_overrides',
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
        'tasks',
        'testers',
        'topics',
        'trends',
        'usage_history',
        'users',
        'work_intent_receipts',
        'workflow_mutation_receipts',
        'workstreams',
        'wrapped',
        'x_connector_users',
        'x_posts',
    }
)


# ---------------------------------------------------------------------------
# Composite indexes derived from database.firestore_index_registry
# ---------------------------------------------------------------------------
#
# The repo's Firestore index registry declares the composite indexes its
# compound queries need (database/firestore_index_registry.INDEX_REQUIREMENTS).
# The shim mirrors those as PostgreSQL expression indexes so the same queries
# are served by real indexes. Expression index columns mirror the query layer:
#   - flat field            -> (data ->> 'field')
#   - dotted field          -> (data #>> '{a,b}')
#   - '__name__' (doc id)   -> doc_id
#   - array 'CONTAINS'      -> (data -> 'field')  (gin)
#
# A registry requirement maps to a table only when its collection_group is a
# known shim table (collection-group queries query the whole table; per-user
# namespace is the uid column).


def _pg_index_expr(field: Any) -> Optional[str]:
    path = field.field_path
    if path == "__name__":
        return "doc_id"
    parsed = parse_field_path(path, allow_document_name=False)
    if getattr(field, "array_config", None) == "CONTAINS":
        if len(parsed) > 1:
            segs = ",".join(parsed)
            return f"(data #> '{{{segs}}}')"
        return f"(data -> '{next(iter(parsed))}')"
    if len(parsed) > 1:
        segs = ",".join(parsed)
        return f"(data #>> '{{{segs}}}')"
    return f"(data ->> '{next(iter(parsed))}')"


def create_composite_indexes(conn: Connection, table_name_for_collection: Callable[[str], str]) -> int:
    """Create PG composite indexes mirroring firestore_index_registry.

    Idempotent (CREATE INDEX IF NOT EXISTS); returns the number of index DDLs
    issued. Only requirements whose collection_group maps to a known shim table
    are created — unknown groups are skipped (a later table use can still be
    served by expression scans).
    """
    created = 0
    for req in INDEX_REQUIREMENTS:
        table = table_name_for_collection(req.collection_group)
        fields = [f for f in req.fields if f.field_path != "__name__"]
        if not fields:
            continue
        exists = conn.execute(
            text("SELECT 1 FROM pg_catalog.pg_class WHERE relname = :t"),
            {"t": table},
        ).fetchone()
        if not exists:
            continue
        index_name = f"idx_{table}_fs_{req.identifier[-40:]}"
        scalar_exprs = []
        array_exprs = []
        for field in fields:
            expr = _pg_index_expr(field)
            if expr is None:
                continue
            if getattr(field, "array_config", None) == "CONTAINS":
                array_exprs.append(expr)
            else:
                scalar_exprs.append(expr)
        if scalar_exprs:
            sql = f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} USING btree (uid, {', '.join(scalar_exprs)})"
            conn.execute(text(sql))
            created += 1
        if array_exprs:
            array_name = f"{index_name}_gin"
            sql = f"CREATE INDEX IF NOT EXISTS {array_name} ON {table} USING gin ({', '.join(array_exprs)})"
            conn.execute(text(sql))
            created += 1
    if created:
        logger.info("firestore_pg: composite indexes migrated (%d)", created)
    return created


# Thread-local transaction connection. ``@firestore.transactional`` bodies run
# on one thread (executors in this codebase are thread-backed), so a TLS slot
# keeps the same PG connection across transaction.get/set/update calls.
_local = threading.local()


def set_tx_conn(conn: Connection) -> None:
    _local.tx_conn = conn


def get_tx_conn() -> Optional[Connection]:
    return getattr(_local, "tx_conn", None)


def clear_tx_conn() -> None:
    _local.tx_conn = None
