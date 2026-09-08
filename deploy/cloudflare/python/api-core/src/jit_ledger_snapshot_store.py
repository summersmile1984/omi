"""D1 reads for upstream proof-gated prompt and paginated mirror snapshots.

No read creates completion, changes writer mode or publishes projection state.
The migration producer must join those records to an actual completed sweep.
"""

import json

from memory_apply_intake import MODEL_COLUMNS
from memory_apply_item import read_item
from memory_history_wire import memory_item_to_memorydb
from memory_kernel_apply import MemoryControlState
from memory_read_authority import HEAD, digest
from jit_trigger_snapshot_kernel import V3TrustedAccountGenerationResult
from jit_ledger_snapshot_kernel import (
    LedgerMigrationCompletion,
    LedgerPromptProjectionReceipt,
    UniversalListCursorError,
    WriterMode,
    _read_fence,
    _prompt_eligible,
    _bounded_prompt_projection,
)

MAX_RECORD_BYTES = 1_000_000
MAX_READ_BYTES = 4_000_000
COLUMNS = sorted(
    set(MODEL_COLUMNS.values()) | {'canonical_metadata_json', 'is_locked', 'user_review', 'deleted_at', 'invalid_at'}
)
DOCUMENT = 'json_object(' + ','.join("'" + name + "'," + name for name in COLUMNS) + ')'
AUTHORITY = '''SELECT h.*,s.completion_json,
CASE WHEN length(CAST(s.projection_json AS BLOB))<=1000000 THEN s.projection_json END AS projection_json,
COALESCE(length(CAST(s.projection_json AS BLOB))>1000000,0) AS projection_oversized
FROM (''' + HEAD + ''') h LEFT JOIN cf_knowledge_ledger_snapshots s ON s.uid=?'''


def bounded_rows(where):
    return (
        '''WITH cohort AS MATERIALIZED (
SELECT id,'''
        + DOCUMENT
        + ' AS document FROM cf_memories WHERE uid=? AND '
        + where
        + '''), sized AS (
SELECT *,SUM(length(CAST(document AS BLOB))) OVER (ORDER BY id) AS bytes FROM cohort)
SELECT id,CASE WHEN length(CAST(document AS BLOB))<=1000000 AND bytes<=4000000
THEN document END AS document FROM sized ORDER BY id'''
    )


PAGE = bounded_rows('(? IS NULL OR id>?) ORDER BY id LIMIT ?')
PROJECTION = bounded_rows('id IN (SELECT value FROM json_each(?)) ORDER BY id')


def stable_projection(rows):
    # These three fields describe a time-dependent read, not persisted content.
    # Return the original receipt unchanged; validate every stored field below.
    return {row.id: row.model_dump(mode='json', exclude={'currency', 'currency_band', 'as_of'}) for row in rows}


class LedgerSnapshotStore:
    def __init__(self, env, uid):
        self.env, self.db, self.uid = env, env.APP_DB, uid
        self.fingerprint = None
        self.current = None
        self.pages = []
        self.receipt_rows = None
        self.receipt = None
        self.bytes = 0

    def secret(self):
        value = getattr(self.env, 'MEMORY_V3_CURSOR_SECRET', '')
        if not isinstance(value, str) or not value:
            raise UniversalListCursorError('missing_cursor_secret')
        return value.encode('utf-8')

    async def authority(self):
        row = await self.db.prepare(AUTHORITY).bind(self.uid, self.uid).first()
        if not row or row['deleted'] or row['projection_oversized']:
            raise ValueError('ledger snapshot authority unavailable')
        if row['control_json'] is None:
            if row['has_memory']:
                raise ValueError('ledger snapshot control unavailable')
            self.current = None
        else:
            current = MemoryControlState.model_validate_json(row['control_json'])
            if (
                current.uid != self.uid
                or current.account_generation != row['trusted_generation']
                or any(
                    getattr(current, field) != row[field]
                    for field in ('account_generation', 'source_generation', 'head_commit_id', 'commit_sequence')
                )
            ):
                raise ValueError('ledger snapshot control identity changed')
            self.current = current
        fingerprint = digest(row)
        if self.fingerprint is not None and fingerprint != self.fingerprint:
            raise ValueError('ledger snapshot authority changed')
        self.fingerprint = fingerprint
        return row

    async def completion(self):
        row = await self.authority()
        if row['completion_json'] is None or self.current is None:
            return None
        try:
            completion = LedgerMigrationCompletion.model_validate_json(row['completion_json'])
            completion.validate_complete()
        except (ValueError, TypeError):
            return None
        if (
            self.current.writer_mode != WriterMode.ledger
            or self.current.writer_epoch != completion.writer_epoch
            or self.current.head_commit_id != completion.source_head_commit_id
        ):
            return None
        return completion

    def item(self, row):
        item = read_item(row)
        if row['deleted_at'] is not None and item.status.value != 'tombstoned':
            raise ValueError('deleted mirror row retains content')
        promotion = dict(item.promotion or {})
        promotion['is_locked'] = bool(row['is_locked']) or bool(promotion.get('is_locked'))
        if row['user_review'] == 0:
            promotion['user_review'] = False
        return item.model_copy(update={'promotion': promotion})

    async def projection_rows(self, receipt):
        ids = sorted(row.id for row in receipt.rows)
        actual = []
        fingerprints = {}
        for start in range(0, len(ids), 16):
            result = await self.db.prepare(PROJECTION).bind(self.uid, json.dumps(ids[start : start + 16])).all()
            for raw in result['results']:
                if raw['document'] is None:
                    raise ValueError('ledger snapshot source exceeds read budget')
                size = len(raw['document'].encode())
                self.bytes += size
                if size > MAX_RECORD_BYTES or self.bytes > MAX_READ_BYTES:
                    raise ValueError('ledger snapshot read budget exhausted')
                row = json.loads(raw['document'])
                item = self.item(row)
                if item.uid != self.uid or item.account_generation != receipt.account_generation:
                    raise ValueError('ledger snapshot source identity changed')
                projected = memory_item_to_memorydb(item)
                if not _prompt_eligible(projected):
                    raise ValueError('ledger snapshot source is no longer eligible')
                actual.append(projected)
                fingerprints[row['id']] = digest(row)
        if stable_projection(_bounded_prompt_projection(actual)) != stable_projection(receipt.rows):
            raise ValueError('ledger snapshot projection changed')
        if self.receipt_rows is not None and self.receipt_rows != fingerprints:
            raise ValueError('ledger snapshot source changed')
        self.receipt_rows = fingerprints

    async def projection(self, completion, *, validate_rows=True):
        row = await self.authority()
        if row['projection_json'] is None or self.current is None:
            return None
        try:
            receipt = LedgerPromptProjectionReceipt.model_validate_json(row['projection_json'])
            receipt.validate_authoritative(uid=self.uid, completion=completion, control=self.current)
            if validate_rows:
                await self.projection_rows(receipt)
                self.receipt = receipt
        except (ValueError, TypeError):
            return None
        return receipt

    async def trusted(self):
        await self.authority()
        if self.current is None:
            raise ValueError('ledger snapshot current head unavailable')
        return V3TrustedAccountGenerationResult(
            uid=self.uid,
            source_path='cf_account_cutover/cf_memory_apply_control',
            account_generation=self.current.account_generation,
            head_commit_id=self.current.head_commit_id,
            commit_sequence=self.current.commit_sequence,
        )

    async def raw_rows(self, last, limit):
        result = await self.db.prepare(PAGE).bind(self.uid, last, last, limit).all()
        if not isinstance(result, dict) or not isinstance(result.get('results'), list):
            raise ValueError('ledger mirror query unavailable')
        raw = result['results']
        if any(row['document'] is None for row in raw):
            raise ValueError('ledger mirror page exceeds read budget')
        return raw

    async def rows(self, last, limit):
        raw = await self.raw_rows(last, limit)
        self.pages.append((last, limit, digest(raw)))
        self.bytes += sum(len(row['document'].encode()) for row in raw)
        if self.bytes > MAX_READ_BYTES:
            raise ValueError('ledger mirror request exceeds read budget')
        values = []
        for row in raw:
            try:
                item = self.item(json.loads(row['document'])).model_dump(mode='python')
            except (ValueError, TypeError, KeyError):
                item = None
            values.append({'id': row['id'], 'item': item})
        return values

    async def fence(self):
        try:
            fence = await _read_fence(self.uid, store=self)
            for last, limit, expected in self.pages:
                if digest(await self.raw_rows(last, limit)) != expected:
                    return None
            return fence
        except Exception:
            return None

    async def verify_prompt(self):
        if self.receipt is None:
            raise ValueError('ledger snapshot has no validated projection')
        await self.authority()
        await self.projection_rows(self.receipt)
