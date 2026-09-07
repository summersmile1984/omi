"""Remove deterministic memory/history identities after observed provider erasure.

The target IDs come only from the preparation owner's immutable inventory. All
history and physical-row deletes share the provider/hold/gate assertion in D1.
Original source conversations and imported source artifacts remain source data;
this operation deletes memories and their derived history, not those sources.
"""

from memory_privacy_apply import gate_statement

TARGETS = "SELECT json_extract(t.value, '$.id') FROM cf_memory_privacy_deletions d, json_each(d.targets_json) t WHERE d.uid = ?"


def references(column):
    # Match the original recursive _value_references_memory_id: exact string
    # values in objects/arrays, not substring matches or dictionary keys.
    return f"EXISTS (SELECT 1 FROM json_tree({column}) j WHERE j.type = 'text' AND j.atom IN ({TARGETS}))"


def history_statements(db, uid):
    operations = f"SELECT operation_id FROM cf_memory_operations WHERE uid = ? AND {references('operation_json')}"
    # Delete dependencies before their operation IDs disappear. A mixed commit
    # is removed whole, while unrelated authoritative memory rows are retained.
    archive_reviews = (
        "SELECT review_id FROM cf_memory_archive_review_items WHERE uid = ? AND ("
        f"memory_id IN ({TARGETS}) OR {references('row_json')})"
    )
    lifecycle_runs = (
        "SELECT run_id FROM cf_memory_short_term_lifecycle_transitions WHERE uid = ? AND ("
        f"memory_id IN ({TARGETS}) OR {references('audit_metadata_json')})"
    )
    queries = [
        f"DELETE FROM cf_memory_commits WHERE uid = ? AND (operation_id IN ({operations}) OR {references('memory_ids_json')})",
        f"DELETE FROM cf_memory_outbox WHERE uid = ? AND (json_extract(event_json, '$.operation_id') IN ({operations}) OR {references('event_json')})",
        f"DELETE FROM cf_memory_operations WHERE uid = ? AND {references('operation_json')}",
        "DELETE FROM cf_memory_review_queue WHERE uid = ? AND ("
        f"fact_id IN ({TARGETS}) OR source_short_term_id IN ({TARGETS}) OR "
        + ' OR '.join(
            references(column)
            for column in (
                'candidate_json',
                'conflict_with_json',
                'referenced_memory_ids_json',
                "COALESCE(correction_json, 'null')",
            )
        )
        + ')',
        "DELETE FROM cf_memory_non_active_routes WHERE uid = ? AND ("
        + references('source_ids_json')
        + ' OR '
        + references('audit_metadata_json')
        + ')',
        f"DELETE FROM cf_memory_archive_items WHERE uid = ? AND memory_id IN ({TARGETS})",
        f"DELETE FROM cf_memory_archive_applies WHERE uid = ? AND (memory_id IN ({TARGETS}) OR review_id IN ({archive_reviews}))",
        f"DELETE FROM cf_memory_archive_review_batches WHERE uid = ? AND review_id IN ({archive_reviews})",
        f"DELETE FROM cf_memory_archive_review_items WHERE uid = ? AND review_id IN ({archive_reviews})",
        "DELETE FROM cf_memory_short_term_lifecycle_runs WHERE uid = ? AND ("
        + references('result_json')
        + f" OR run_id IN ({lifecycle_runs}))",
        f"DELETE FROM cf_memory_short_term_lifecycle_transitions WHERE uid = ? AND run_id IN ({lifecycle_runs})",
        f"DELETE FROM cf_usage_sources WHERE uid = ? AND source_kind = 'memory' AND source_id IN ({TARGETS})",
    ]
    # All placeholders in these fixed statements are the authenticated owner;
    # target sets are resolved from that owner's immutable cleanup inventory.
    return [db.prepare(sql).bind(*([uid] * sql.count('?'))) for sql in queries]


async def finalize_privacy_deletion(env, uid, token, now):
    db = env.APP_DB
    # Reacquisition is selected from the durable inventory, so a stale retry
    # cannot reopen an already completed gate. Hold placement contends here.
    statements = [
        gate_statement(db, uid, token, now, resume=True),
        db.prepare('INSERT INTO cf_memory_privacy_finalize_guard(uid, token) VALUES (?, ?)').bind(uid, token),
        *history_statements(db, uid),
        db.prepare(f'DELETE FROM cf_memories WHERE uid = ? AND id IN ({TARGETS})').bind(uid, uid),
        # Physical removal emits one final projection task. Provider absence
        # was proved inside this same transaction; no obsolete task survives.
        db.prepare(
            f"DELETE FROM cf_vector_projection_outbox WHERE uid = ? AND source_kind = 'memory' AND source_id IN ({TARGETS})"
        ).bind(uid, uid),
        db.prepare(
            "UPDATE cf_destructive_operation_gates SET state = 'completed', finished_at = ? WHERE uid = ? AND token = ?"
        ).bind(now, uid, token),
        db.prepare('DELETE FROM cf_memory_privacy_deletions WHERE uid = ? AND token = ?').bind(uid, token),
        db.prepare('DELETE FROM cf_memory_privacy_finalize_guard WHERE uid = ?').bind(uid),
    ]
    await db.batch(statements)
