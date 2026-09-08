"""Bounded D1 scan for the original canonical history policy.

Page fingerprints and head/account rechecks also fence historical writers that
have not yet joined the canonical head. No read writes a migration or receipt.
"""

import asyncio
import json

from memory_apply_intake import MODEL_COLUMNS
from memory_apply_item import read_item
from memory_history_kernel import ListReadBudgetExhausted
from memory_kernel_apply import MemoryControlState
from memory_kernel_item import MemoryItemStatus
from memory_read_authority import HEAD, digest

PAGE_ROWS = 32
PAGE_BYTES = 1_000_000
SCAN_BYTES = 4_000_000
_COLUMNS = sorted(
    set(MODEL_COLUMNS.values())
    | {
        'canonical_metadata_json',
        'is_locked',
        'user_review',
        'deleted_at',
        'invalid_at',
    }
)
_DOCUMENT = 'json_object(' + ','.join("'" + name + "'," + name for name in _COLUMNS) + ')'
# Bound transfer bytes in SQL, before crossing the JS/Python bridge. A single
# larger D1 row is still read, charged and either admitted or marked truncated.
PAGE = (
    '''WITH cohort AS MATERIALIZED (
SELECT id,updated_at,'''
    + _DOCUMENT
    + ''' AS document FROM cf_memories
WHERE uid=? AND canonical_metadata_json!='{}' AND
(? IS NULL OR updated_at<? OR (updated_at=? AND id>?))
ORDER BY updated_at DESC,id LIMIT ?), sized AS (
SELECT *,SUM(length(CAST(document AS BLOB))) OVER (ORDER BY updated_at DESC,id) AS bytes,
ROW_NUMBER() OVER (ORDER BY updated_at DESC,id) AS row_number FROM cohort)
SELECT document FROM sized WHERE bytes<=? OR row_number=1 ORDER BY updated_at DESC,id'''
)


class HistoryStore:
    def __init__(self, env, uid):
        self.db, self.uid = env.APP_DB, uid
        self.authority, self.generation = None, None
        self.pages = []

    async def head(self):
        row = await self.db.prepare(HEAD).bind(self.uid).first()
        if not row or row['deleted']:
            raise ValueError('memory history authority unavailable')
        generation = row['trusted_generation']
        if type(generation) is not int or generation < 0:
            raise ValueError('memory history generation unavailable')
        if row['control_json'] is None:
            if row['has_memory']:
                raise ValueError('memory history control unavailable')
        else:
            control = MemoryControlState.model_validate_json(row['control_json'])
            if (
                control.uid != self.uid
                or control.account_generation != generation
                or any(
                    row[field] != getattr(control, field)
                    for field in ('account_generation', 'head_commit_id', 'commit_sequence', 'source_generation')
                )
            ):
                raise ValueError('memory history control changed')
        fingerprint = digest(row)
        if self.authority is not None and fingerprint != self.authority:
            raise ValueError('memory history authority changed')
        self.authority, self.generation = fingerprint, generation

    async def page(self, cursor, limit):
        stamp, identity = cursor
        result = await self.db.prepare(PAGE).bind(self.uid, stamp, stamp, stamp, identity, limit, PAGE_BYTES).all()
        if not isinstance(result, dict) or not isinstance(result.get('results'), list):
            raise ValueError('memory history query unavailable')
        return result['results']

    async def items(self, limit, budget):
        cursor, count, transferred = (None, ''), 0, 0
        while count < limit:
            requested = min(PAGE_ROWS, limit - count)
            try:
                async with asyncio.timeout(budget.rpc_timeout()):
                    records = await self.page(cursor, requested)
            except TimeoutError as exc:
                budget.mark_exhausted('deadline')
                raise ListReadBudgetExhausted('deadline') from exc
            self.pages.append((cursor, requested, digest(records)))
            if not records:
                return
            for record in records:
                budget.charge(1)
                transferred += len(record['document'].encode())
                if transferred > SCAN_BYTES:
                    budget.mark_exhausted('bytes')
                    raise ListReadBudgetExhausted('bytes')
                row = json.loads(record['document'])
                cursor = (row['updated_at'], row['id'])
                count += 1
                item = read_item(row)
                if item.uid != self.uid or item.account_generation != self.generation:
                    raise ValueError('memory history row authority changed')
                # Physical privacy and subscription gates remain authoritative
                # while historical product writers converge on canonical apply.
                if row['deleted_at'] is not None:
                    item = item.model_copy(update={'status': MemoryItemStatus.tombstoned})
                promotion = dict(item.promotion or {})
                promotion['is_locked'] = bool(row['is_locked']) or bool(promotion.get('is_locked'))
                if row['user_review'] == 0 or promotion.get('user_review') is False:
                    promotion['user_review'] = False
                elif row['user_review'] is not None:
                    promotion['user_review'] = bool(row['user_review'])
                yield item.model_copy(update={'promotion': promotion})

    async def verify(self):
        await self.head()
        # The head alone cannot see all pre-convergence writes. Re-read the
        # exact bounded cohorts, one page at a time, without retaining content.
        for cursor, limit, expected in self.pages:
            if digest(await self.page(cursor, limit)) != expected:
                raise ValueError('memory history rows changed')
        await self.head()
