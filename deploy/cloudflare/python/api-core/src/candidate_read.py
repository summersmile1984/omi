"""Generation-scoped Candidate reads with the unchanged Suggested projection."""

from datetime import datetime, timezone
import json

from pydantic import ValidationError
from candidate_create import parse_record
from candidate_db import account_generation
from candidate_kernel_policy import CandidateGenerationMismatchError
from candidate_kernel_suggested import SUGGESTED_CANDIDATE_RAW_LIMIT, _suggested_candidates
from fallback import record_fallback

# D1's generated epoch column stores seconds. Retain subsecond ordering from the
# original record when a batch creates several suggestions in one second.
_ORDER = (
    "created_at DESC, CASE WHEN substr(json_extract(record_json,'$.created_at'),20,1)='.' "
    "THEN CAST('0'||substr(json_extract(record_json,'$.created_at'),20) AS REAL) ELSE 0 END DESC, candidate_id DESC"
)


def parse_read(raw):
    try:
        return parse_record(raw)
    except ValidationError:
        record_fallback(from_mode='none', to_mode='none', reason='malformed_doc', outcome='degraded')
        return None


async def records(env, uid, *, generation, status=None, limit=100, offset=0):
    query = 'SELECT record_json FROM cf_candidates WHERE uid=? AND account_generation=?'
    params = [uid, generation]
    if status is not None:
        query += ' AND status=?'
        params.append(status.value)
    result = (
        await env.APP_DB.prepare(query + ' ORDER BY ' + _ORDER + ' LIMIT ? OFFSET ?').bind(*params, limit, offset).all()
    )
    return [record for row in result['results'] if (record := parse_read(row['record_json'])) is not None]


async def list_candidates(env, uid, *, status=None, limit=100, offset=0, surface=None, now=None):
    generation = await account_generation(env, uid)
    if surface == 'suggested':
        candidates = await records(env, uid, generation=generation, limit=SUGGESTED_CANDIDATE_RAW_LIMIT)
        current = now or datetime.now(timezone.utc)
        overrides = (
            await env.APP_DB.prepare(
                'SELECT record_json FROM cf_task_attention_overrides WHERE uid=? AND account_generation=? AND expires_at>=?'
            )
            .bind(uid, generation, int(current.timestamp()))
            .all()
        )
        suppressed = {
            value['dedupe_key']
            for row in overrides['results']
            if (value := json.loads(row['record_json'])).get('dedupe_key')
            and datetime.fromisoformat(value['expires_at']) > current
        }
        selected = _suggested_candidates(candidates, limit=limit, suppressed_dedupe_keys=suppressed, now=current)
        has_more = False
    else:
        candidates = await records(env, uid, generation=generation, status=status, limit=limit + 1, offset=offset)
        selected, has_more = candidates[:limit], len(candidates) > limit
    if await account_generation(env, uid) != generation:
        raise CandidateGenerationMismatchError('account generation changed while reading Candidates')
    return selected, has_more


async def get_owned_candidate(env, uid, candidate_id):
    generation = await account_generation(env, uid)
    row = (
        await env.APP_DB.prepare(
            'SELECT record_json FROM cf_candidates WHERE uid=? AND candidate_id=? AND account_generation=?'
        )
        .bind(uid, candidate_id, generation)
        .first()
    )
    record = parse_read(row['record_json']) if row is not None else None
    if await account_generation(env, uid) != generation:
        raise CandidateGenerationMismatchError('account generation changed while reading Candidate')
    return record
