"""Bounded recap context and source-owned facts, never model-authored records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json

from fallback import record_fallback
from memory_routes import ARCHIVE_RESTRICTED_SENSITIVITY_LABELS

MAX_HISTORY_CHARS = 24_000
CONVERSATION_FIELDS = (
    "id",
    "started_at",
    "finished_at",
    "created_at",
    "structured_json",
    "apps_results_json",
    "transcript_segments_json",
    "geolocation_json",
    "discarded",
    "is_locked",
)
TASK_FIELDS = ("id", "description", "completed", "conversation_id", "deleted", "is_locked", "status")
MEMORY_FIELDS = (
    "id",
    "content",
    "category",
    "created_at",
    "capture_confidence",
    "veracity",
    "conversation_id",
    "evidence_json",
    "item_revision",
    "deleted_at",
    "invalid_at",
    "superseded_by",
    "is_locked",
    "user_review",
    "memory_tier",
    "status",
    "processing_state",
    "source_state",
    "sensitivity_labels_json",
    "account_generation",
    "expires_at",
)
MEMORY_ELIGIBILITY = (
    "deleted_at IS NULL AND invalid_at IS NULL AND COALESCE(superseded_by, '') = '' "
    "AND is_locked = 0 AND COALESCE(user_review, 1) != 0 AND memory_tier != 'archive' "
    "AND status = 'active' AND processing_state = 'processed' AND source_state = 'active' "
    "AND (expires_at IS NULL OR expires_at > unixepoch()) "
    "AND trim(content) != '' AND json_type(sensitivity_labels_json) = 'array' "
    "AND NOT EXISTS (SELECT 1 FROM json_each(sensitivity_labels_json) label WHERE "
    "lower(trim(CAST(label.value AS TEXT))) IN ("
    + ",".join("'" + label + "'" for label in ARCHIVE_RESTRICTED_SENSITIVITY_LABELS)
    + ")) "
    "AND account_generation = COALESCE((SELECT account_generation FROM cf_account_cutover "
    "WHERE cf_account_cutover.uid = cf_memories.uid), 0)"
)


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode(value: object, default):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, type(default)) else default
    except (TypeError, ValueError):
        return default


def degraded(reason="other"):
    record_fallback(component="other", from_mode="none", to_mode="none", reason=reason, outcome="degraded")


async def rows(env, sql, *args):
    result = await env.APP_DB.prepare(sql).bind(*args).all()
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise RuntimeError("recap data unavailable")
    return result["results"]


def conversation_text(row):
    structured = decode(row.get("structured_json"), {})
    apps = decode(row.get("apps_results_json"), [])
    app_content = apps[0].get("content", "") if apps and isinstance(apps[0], dict) else ""
    overview = app_content or structured.get("overview", "")
    items = structured.get("action_items") or []
    events = structured.get("events") or []
    if not (overview or items or events):
        return None
    return {"title": structured.get("title", ""), "overview": overview, "action_items": items, "events": events}


def learned_memories(candidates, conversation_ids, start, end):
    selected = []
    for row in candidates:
        evidence = {row["conversation_id"]} if row.get("conversation_id") else set()
        for item in decode(row.get("evidence_json"), []):
            if isinstance(item, dict) and item.get("source_type") == "conversation" and item.get("source_id"):
                evidence.add(item["source_id"])
        if (evidence and not evidence.intersection(conversation_ids)) or (
            not evidence and not start <= row["created_at"] < end
        ):
            continue
        selected.append(row)
    selected.sort(
        key=lambda r: (-(r.get("capture_confidence") or 0), -(r.get("veracity") or 0), -r["created_at"], r["id"])
    )
    return selected[:3]


@dataclass
class SummaryContent:
    facts: dict
    prompt: str
    conversations: list[dict]
    tasks: list[dict]
    memories: list[dict]

    def persistence_guard(self, uid):
        clauses, args = [], []
        for table, fields, records in (
            ("cf_conversations", CONVERSATION_FIELDS, self.conversations),
            ("cf_action_items", TASK_FIELDS, self.tasks),
            ("cf_memories", MEMORY_FIELDS, self.memories),
        ):
            snapshots = [{field: row.get(field) for field in fields} for row in records]
            comparisons = " AND ".join(f"live.{field} IS json_extract(wanted.value, '$.{field}')" for field in fields)
            if table == "cf_memories":
                comparisons += " AND (live.expires_at IS NULL OR live.expires_at > unixepoch())"
            clauses.append(
                f"NOT EXISTS (SELECT 1 FROM json_each(?) wanted WHERE NOT EXISTS "
                f"(SELECT 1 FROM {table} live WHERE live.uid = ? AND {comparisons}))"
            )
            args.extend([encode(snapshots), uid])
        return " AND ".join(clauses), args


async def load_content(env, uid, date_text, start, end, zone) -> SummaryContent | None:
    candidates = await rows(
        env,
        "SELECT " + ",".join(CONVERSATION_FIELDS) + " FROM cf_conversations "
        "WHERE uid = ? AND discarded = 0 AND is_locked = 0 AND started_at >= ? AND started_at < ? "
        "ORDER BY started_at DESC, id DESC LIMIT 201",
        uid,
        start,
        end,
    )
    if not any(decode(row.get("transcript_segments_json"), []) for row in candidates):
        return None
    conversations, history, size = [], [], 0
    truncated = len(candidates) > 200
    for row in candidates[:200]:
        content = conversation_text(row)
        rendered = encode(content) if content else ""
        if size + len(rendered) > MAX_HISTORY_CHARS and conversations:
            truncated = True
            break
        # Keep at least the latest useful content, including for one huge day item.
        if len(rendered) > MAX_HISTORY_CHARS:
            rendered = rendered[:MAX_HISTORY_CHARS]
            truncated = True
        conversations.append(row)
        history.append({"conversation_number": len(conversations), "content": rendered})
        size += len(rendered)
    if not any(item["content"] for item in history):
        return None
    if truncated:
        degraded()
    ids = [row["id"] for row in conversations]
    tasks = await rows(
        env,
        "SELECT " + ",".join(TASK_FIELDS) + " FROM cf_action_items WHERE uid = ? AND deleted = 0 "
        "AND is_locked = 0 AND status IN ('active', 'completed') "
        "AND conversation_id IN (SELECT value FROM json_each(?)) ORDER BY created_at, id LIMIT 1001",
        uid,
        encode(ids),
    )
    if len(tasks) > 1000:
        raise RuntimeError("daily summary task budget exceeded")
    try:
        memories = learned_memories(
            await rows(
                env,
                "SELECT "
                + ",".join(MEMORY_FIELDS)
                + " FROM cf_memories WHERE uid = ? AND "
                + MEMORY_ELIGIBILITY
                + " ORDER BY updated_at DESC, id ASC LIMIT 600",
                uid,
            ),
            set(ids),
            start,
            end,
        )
    except Exception:
        degraded("dependency_unavailable")
        memories = []
    counts = (
        await env.APP_DB.prepare(
            "SELECT (SELECT COUNT(*) FROM cf_memories WHERE uid = ? AND deleted_at IS NULL "
            "AND created_at >= ? AND created_at < ?) AS memories_created, "
            "(SELECT COUNT(*) FROM cf_action_items WHERE uid = ? AND deleted = 0 AND is_locked = 0 "
            "AND created_at >= ? AND created_at < ?) AS action_items_created, "
            "(SELECT COALESCE(SUM(watching_seconds), 0) FROM cf_desktop_daily_usage WHERE uid = ? AND date = ?) AS watching_seconds, "
            "(SELECT COALESCE(SUM(proactive_cards_shown), 0) FROM cf_desktop_daily_usage WHERE uid = ? AND date = ?) AS proactive_moments"
        )
        .bind(uid, start, end, uid, start, end, uid, date_text, uid, date_text)
        .first()
    )
    if not isinstance(counts, dict):
        raise RuntimeError("daily summary stats unavailable")
    language = (
        await env.APP_DB.prepare("SELECT language FROM cf_user_transcription_preferences WHERE uid = ?")
        .bind(uid)
        .first()
    )
    facts = {
        "date": date_text,
        "stats": {
            "total_conversations": len(conversations),
            "total_duration_minutes": int(
                sum(max(0, (r.get("finished_at") or r["started_at"]) - r["started_at"]) for r in conversations) / 60
            ),
            "action_items_count": len(tasks),
            "memories_created": counts["memories_created"],
            "action_items_created": counts["action_items_created"],
            "watching_minutes": round(counts["watching_seconds"] / 60),
            "proactive_moments": counts["proactive_moments"],
        },
        "action_items": [
            {
                "description": r["description"],
                "priority": "medium" if r["completed"] else "high",
                "completed": bool(r["completed"]),
                "source_conversation_id": r["conversation_id"],
            }
            for r in tasks
        ],
        "memories_learned": [
            {
                "memory_id": r["id"],
                "content": r["content"].strip(),
                "category": r["category"],
                "captured_at": datetime.fromtimestamp(r["created_at"], timezone.utc).isoformat(),
            }
            for r in memories
        ],
        "locations": [],
    }
    for row in conversations:
        geo = decode(row.get("geolocation_json"), {})
        if all(
            isinstance(geo.get(key), (int, float)) and not isinstance(geo[key], bool)
            for key in ("latitude", "longitude")
        ):
            facts["locations"].append(
                {
                    "latitude": geo["latitude"],
                    "longitude": geo["longitude"],
                    "address": geo.get("address"),
                    "conversation_id": row["id"],
                    "time": datetime.fromtimestamp(row["started_at"], zone).strftime("%H:%M"),
                }
            )
    prompt = encode(
        {
            "date": date_text,
            "output_language": (language or {}).get("language") or "en",
            "stats": facts["stats"],
            "conversations": history,
        }
    )
    return SummaryContent(facts, prompt, conversations, tasks, memories)
