"""One guarded D1 append/close transaction for explicit upstream history restore."""

import json
from datetime import datetime, timezone

from fastapi import HTTPException
from memory_apply_item import read_item
from memory_apply_intake import (
    MODEL_COLUMNS,
    _stored_item,
    _insert_rows,
    encoded,
    load_memory_control,
    append_journal_records,
    control_statement,
)
from memory_kernel_apply import ApplyStatus, apply_long_term_patch_transaction
from memory_kernel_item import MemoryItem
from memory_kernel_operations import MemoryLedgerReopenReceipt
from memory_privacy_receipts import privacy_receipt_id
from memory_revert_kernel import MemoryService, _canonical_extraction_apply_write, _legacy_evidence_to_memory
from vector_search import publish_vector_projection

READ_COLUMNS = sorted(
    set(MODEL_COLUMNS.values()) | {'canonical_metadata_json', 'is_locked', 'user_review', 'deleted_at', 'invalid_at'}
)


class RevertConflict(RuntimeError):
    def __init__(self, *, retryable):
        super().__init__('memory restore authority changed')
        self.retryable = retryable


class RevertStore(MemoryService):
    def __init__(self, env, uid):
        self.env, self.db, self.uid = env, env.APP_DB, uid
        self.db_client = self
        self.rows = {}
        self.bytes = 0
        self.prior = None
        self.state = None

    async def ensure_canonical_mutation_ready(self, uid):
        if uid != self.uid:
            raise ValueError('memory restore owner mismatch')
        prior, state = await load_memory_control(self.env, uid)
        if prior is None:
            existing = (
                await self.db.prepare('SELECT 1 AS present FROM cf_memories WHERE uid=? LIMIT 1').bind(uid).first()
            )
            if existing:
                raise HTTPException(503, 'Canonical memory unavailable')
        if self.state is not None and prior != self.prior:
            raise ValueError('memory restore head changed')
        self.prior, self.state = prior, state

    async def control(self, uid, **kwargs):
        await self.ensure_canonical_mutation_ready(uid)
        return self.state

    async def _canonical_item_for_lineage(self, uid, memory_id):
        if uid != self.uid or self.state is None:
            raise ValueError('memory restore authority unavailable')
        row = await self.db.prepare('SELECT * FROM cf_memories WHERE uid=? AND id=?').bind(uid, memory_id).first()
        if row is None:
            return None
        size = len(encoded(row).encode())
        self.bytes += size
        if size > 1_000_000 or self.bytes > 4_000_000:
            raise HTTPException(503, 'Memory restore read budget exhausted')
        if row['canonical_metadata_json'] == '{}':
            raise HTTPException(409, 'Knowledge ledger history cannot be restored')
        item = read_item(row)
        if item.uid != uid or item.memory_id != memory_id or item.account_generation != self.state.account_generation:
            raise ValueError('memory restore identity changed')
        if row['deleted_at'] is not None:
            raise HTTPException(409, 'Knowledge ledger history cannot be restored')
        promotion = dict(item.promotion or {})
        promotion['is_locked'] = bool(row['is_locked']) or bool(promotion.get('is_locked'))
        if row['user_review'] == 0:
            promotion['user_review'] = False
        item = item.model_copy(update={'promotion': promotion})
        previous = self.rows.get(memory_id)
        if previous is not None and any(previous[column] != row[column] for column in READ_COLUMNS):
            raise ValueError('memory restore source changed')
        self.rows[memory_id] = row
        return item

    async def active_item(self, uid, memory_id, **kwargs):
        item = await self._canonical_item_for_lineage(uid, memory_id)
        return item if item is not None and item.status.value == 'active' else None

    async def verify(self):
        # This is also the authority check for an exact replay which made no writes.
        await self.ensure_canonical_mutation_ready(self.uid)
        for key, prior in list(self.rows.items()):
            current = (
                await self.db.prepare('SELECT * FROM cf_memories WHERE uid=? AND id=?').bind(self.uid, key).first()
            )
            if current is None or any(current[column] != prior[column] for column in READ_COLUMNS):
                raise ValueError('memory restore readback changed')

    async def append(self, uid, data, *, required_source_item, ledger_reopen_receipt=None):
        await self.ensure_canonical_mutation_ready(uid)
        source = required_source_item
        if source is None or source.uid != uid or source.memory_id not in self.rows:
            raise ValueError('memory restore source is required')
        if (
            data.get('ledger_schema_version') != 'knowledge_ledger.v1'
            or data.get('write_reason') != 'direct_user_statement'
            or data.get('user_asserted') is not True
            or not data.get('id')
            or not data.get('subject_scope')
            or not (data.get('supersedes') or ledger_reopen_receipt is not None)
        ):
            raise ValueError('memory restore requires explicit user append authority')
        evidence = [_legacy_evidence_to_memory(value, conversation_id=None) for value in data.get('evidence', [])]
        if not evidence or evidence[0].source_type not in {'explicit_user_revert', 'explicit_user_reopen'}:
            raise ValueError('memory restore evidence unavailable')
        if source.source_state.value != 'active' or any(
            e.source_state.value != 'active'
            or e.redaction_status.value in {'redacted', 'tombstoned'}
            or e.encryption_or_redaction_status != 'active'
            for e in source.evidence
        ):
            raise ValueError('memory restore source is retired')
        receipt = None
        if ledger_reopen_receipt is not None:
            receipt = MemoryLedgerReopenReceipt.model_validate(ledger_reopen_receipt.model_dump())
            if (
                receipt.uid != uid
                or receipt.source_memory_id != source.memory_id
                or receipt.replacement_memory_id != data['id']
                or receipt.account_generation != self.state.account_generation
                or receipt.source_generation != self.state.source_generation
                or receipt.source_item_revision != source.item_revision
                or receipt.source_content_hash != source.content_hash
            ):
                raise ValueError('memory reopen receipt changed')
        superseded = []
        for key in data.get('supersedes', []):
            item = await self._canonical_item_for_lineage(uid, key)
            if item is None or item.status.value != 'active':
                raise ValueError('memory restore current tail changed')
            superseded.append(item)
        write, memory_id = _canonical_extraction_apply_write(uid, data, control=self.state, evidence_items=evidence)
        result = apply_long_term_patch_transaction(
            control_state=self.state,
            operation=write.operation,
            patch_payload={**write.patch_payload, 'evidence': evidence, 'superseded_items': superseded},
        )
        if result.status != ApplyStatus.committed:
            raise ValueError('memory restore apply was not admitted: ' + result.status.value)
        now = int(datetime.now(timezone.utc).timestamp())
        statements = [
            self.db.prepare(
                'INSERT INTO cf_memory_apply_guard(uid,expected_control_json,account_generation,new_ids_json,operation_ids_json,expected_items_json) VALUES (?,?,?,?,?,?)'
            ).bind(
                uid,
                self.prior,
                self.state.account_generation,
                encoded([memory_id]),
                encoded([write.operation.operation_id]),
                encoded([self.rows[item.memory_id] for item in superseded]),
            )
        ]
        statements.extend(
            _insert_rows(
                self.db,
                'cf_memory_ledger_read_guard',
                [
                    {
                        'uid': uid,
                        'memory_id': key,
                        'snapshot_json': encoded({column: row[column] for column in READ_COLUMNS}),
                    }
                    for key, row in self.rows.items()
                ],
            )
        )
        columns = sorted((set(MODEL_COLUMNS.values()) | {'canonical_metadata_json'}) - {'uid', 'id'})
        stored_items = {}
        for raw in result.memory_items:
            item = MemoryItem.model_validate(raw.model_dump())
            prior_row = self.rows.get(item.memory_id)
            base = prior_row or {
                'created_at': now,
                'valid_at': now,
                'privacy_receipt_id': privacy_receipt_id(self.env, uid, item.memory_id),
            }
            stored = _stored_item(base, item)
            stored_items[item.memory_id] = stored
            if prior_row is None:
                statements.extend(_insert_rows(self.db, 'cf_memories', [stored]))
            else:
                statements.append(
                    self.db.prepare(
                        'UPDATE cf_memories SET '
                        + ','.join(column + '=?' for column in columns)
                        + ' WHERE uid=? AND id=?'
                    ).bind(*(stored[column] for column in columns), uid, item.memory_id)
                )
        statements.extend(
            _insert_rows(
                self.db,
                'cf_memory_graph_assertions',
                [
                    {
                        'uid': uid,
                        'memory_id': assertion.memory_id,
                        'item_revision': assertion.item_revision,
                        'account_generation': self.state.account_generation,
                        'assertion_json': assertion.model_dump_json(),
                    }
                    for assertion in result.graph_assertions
                ],
            )
        )
        records = {table: [] for table in ('cf_memory_operations', 'cf_memory_commits', 'cf_memory_outbox')}
        append_journal_records(records, uid, result, self.state, now)
        for table, values in records.items():
            statements.extend(_insert_rows(self.db, table, values))
        statements.append(control_statement(self.db, uid, result.control_state))
        if receipt:
            statements.extend(
                _insert_rows(
                    self.db,
                    'cf_memory_ledger_reopens',
                    [
                        {
                            'uid': uid,
                            'source_memory_id': source.memory_id,
                            'replacement_memory_id': memory_id,
                            'account_generation': self.state.account_generation,
                            'receipt_json': receipt.model_dump_json(),
                        }
                    ],
                )
            )
        statements.extend(
            [
                self.db.prepare('DELETE FROM cf_memory_ledger_read_guard WHERE uid=?').bind(uid),
                self.db.prepare('DELETE FROM cf_memory_apply_guard WHERE uid=?').bind(uid),
            ]
        )
        try:
            await self.db.batch(statements)
        except Exception as exc:
            message = str(exc)
            if 'UNIQUE constraint failed: cf_memory_ledger_reopens.' in message or 'memory_privacy_deleted' in message:
                raise RevertConflict(retryable=False) from exc
            if any(
                code in message
                for code in (
                    'memory_apply_head_changed',
                    'memory_apply_target_exists',
                    'memory_apply_operation_changed',
                    'memory_apply_target_changed',
                    'memory_locked_for_mutation',
                    'memory_ledger_source_changed',
                )
            ):
                raise RevertConflict(retryable=True) from exc
            raise
        self.prior, self.state = await load_memory_control(self.env, uid)
        if self.state.head_commit_id != result.control_state.head_commit_id:
            raise ValueError('memory restore commit advanced before readback')
        # The original service re-reads replacement and closed tail after commit.
        for key in stored_items:
            self.rows.pop(key, None)
        for key in stored_items:
            await publish_vector_projection(self.env, uid=uid, source_kind='memory', source_id=key)
        return memory_id
