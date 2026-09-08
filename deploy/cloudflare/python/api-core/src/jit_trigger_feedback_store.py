"""Feedback joins the canonical memory owner and existing Candidate event guard."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from candidate_db import CandidateTransaction
from jit_proactivity_models import JITProactivityEventReceipt
from jit_trigger_feedback_models import JITTriggerFeedbackReceipt
from memory_apply_intake import encoded, read_memory_control
from memory_apply_item import read_item
from memory_apply_mutation import apply_user_memory_mutation


@dataclass
class CanonicalTriggerFeedback:
    transaction: CandidateTransaction
    proposed: JITTriggerFeedbackReceipt
    event: JITProactivityEventReceipt
    item: object = None

    def validate(self, item, control, logical, patch):
        receipt = self.proposed
        if (
            receipt.uid != item.uid
            or receipt.uid != self.transaction.uid
            or receipt.trigger_memory_id != item.memory_id
            or receipt.account_generation != item.account_generation
            or receipt.account_generation != control.account_generation
            or receipt.expected_trigger_revision != item.item_revision
            or item.kind.value != 'trigger'
            or item.ledger_schema_version != 'knowledge_ledger.v1'
            or set(logical) != {'result_status'}
            or set(patch) != {'arguments', 'curation_weight'}
            or 'jit_trigger_feedback' not in patch['arguments']
            or {k: v for k, v in patch['arguments'].items() if k != 'jit_trigger_feedback'}
            != {k: v for k, v in item.arguments.items() if k != 'jit_trigger_feedback'}
        ):
            raise ValueError('trigger feedback authority changed')

    def statements(self, updated):
        self.item = updated
        receipt = self.proposed.model_copy(update={'applied_trigger_revision': updated.item_revision})
        self.transaction.put(
            'jit_events',
            self.event.event_id,
            self.event.model_copy(update={'feedback_id': receipt.feedback_id}).model_dump(mode='json'),
        )
        self.transaction.put('jit_feedback', receipt.feedback_id, receipt.model_dump(mode='json'))
        return self.transaction.prepared_batch()


class FeedbackStore:
    def __init__(self, env, uid):
        self.env, self.uid = env, uid
        self.tx = None

    async def row(self, memory_id):
        return (
            await self.env.APP_DB.prepare('SELECT * FROM cf_memories WHERE uid=? AND id=?')
            .bind(self.uid, memory_id)
            .first()
        )

    async def item(self, memory_id):
        row = await self.row(memory_id)
        return read_item(row) if row else None

    async def replay(self, feedback_id, request_hash):
        prior, control = await read_memory_control(self.env, self.uid)
        if prior is None:
            raise ValueError('trigger feedback control unavailable')
        self.tx = CandidateTransaction(self.env, self.uid, control.account_generation)
        raw = await self.tx.record('jit_feedback', feedback_id)
        if raw is None:
            return None
        receipt = JITTriggerFeedbackReceipt.model_validate_json(raw)
        if (
            receipt.uid != self.uid
            or receipt.request_hash != request_hash
            or receipt.account_generation != control.account_generation
            or receipt.applied_trigger_revision is None
        ):
            raise ValueError('trigger feedback replay receipt unavailable')
        await self.event(receipt, replay=True)
        row = await self.row(receipt.trigger_memory_id)
        item = read_item(row) if row else None
        if (
            item is None
            or item.uid != self.uid
            or item.kind.value != 'trigger'
            or item.account_generation != receipt.account_generation
            or item.item_revision < receipt.applied_trigger_revision
        ):
            raise ValueError('trigger feedback replay target unavailable')
        # The same guarded batch proves the receipt, event, current target and
        # account/head together, including a hidden target after disable.
        db = self.env.APP_DB
        await db.batch(
            [
                db.prepare(
                    'INSERT INTO cf_memory_apply_guard(uid,expected_control_json,account_generation,new_ids_json,operation_ids_json,observed_items_json) VALUES (?,?,?,?,?,?)'
                ).bind(self.uid, prior, control.account_generation, '[]', '[]', encoded([row])),
                *self.tx.prepared_batch(),
                db.prepare('DELETE FROM cf_memory_apply_guard WHERE uid=?').bind(self.uid),
            ]
        )
        return item, receipt

    async def event(self, receipt, *, replay=False):
        raw = await self.tx.record('jit_events', receipt.event_id)
        if raw is None:
            raise ValueError('trigger feedback event unavailable')
        event = JITProactivityEventReceipt.model_validate_json(raw)
        if (
            event.uid != self.uid
            or event.account_generation != receipt.account_generation
            or event.trigger_memory_id != receipt.trigger_memory_id
            or event.trigger_revision != receipt.expected_trigger_revision
            or (
                event.feedback_id != receipt.feedback_id
                if replay
                else event.operation != 'planned_notification' or event.feedback_id not in {None, receipt.feedback_id}
            )
        ):
            raise ValueError('trigger feedback event authority changed')
        return event

    async def apply(self, memory_id, build_patch, receipt):
        event = await self.event(receipt)
        participant = CanonicalTriggerFeedback(self.tx, receipt, event)

        def patch(item, now):
            logical, updates = build_patch(item, now)
            return logical, updates, {}

        found = await apply_user_memory_mutation(
            self.env,
            self.uid,
            memory_id,
            datetime.now(timezone.utc).timestamp(),
            kind='jit_trigger_feedback:' + receipt.feedback_id,
            build_patch=patch,
            trigger_feedback=participant,
        )
        if not found:
            raise ValueError('trigger feedback target unavailable')
        return None, participant.item

    async def receipt(self, feedback_id):
        row = (
            await self.env.APP_DB.prepare(
                'SELECT record_json FROM cf_jit_trigger_feedback WHERE uid=? AND feedback_id=?'
            )
            .bind(self.uid, feedback_id)
            .first()
        )
        return SimpleNamespace(exists=row is not None, to_dict=lambda: json.loads(row['record_json']) if row else {})
