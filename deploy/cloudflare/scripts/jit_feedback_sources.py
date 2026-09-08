"""Keep upstream feedback policy and wire models; relocate only persistence IO."""

import ast

from screen_frame_sources import selected_nodes, source

OWNER = 'backend/utils/memory/canonical_memory_adapter.py'


def feedback_policy():
    tree = ast.parse(source(OWNER))
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'apply_canonical_trigger_feedback'
    )
    fn.args.kwonlyargs[-1].arg = 'store'
    changes = set()

    class Storage(ast.NodeTransformer):
        def visit_Assign(self, node):
            name = ast.unparse(node.targets[0])
            replacements = {
                'replay': 'await store.replay(parsed_feedback.feedback_id, request_hash)',
                'initial': 'await store.item(normalized_memory_id)',
                '(_, updated)': 'await store.apply(normalized_memory_id, build_patch, proposed_receipt)',
                'committed_snapshot': 'await store.receipt(parsed_feedback.feedback_id)',
            }
            if name == 'client':
                changes.add(name)
                return None
            if name in replacements:
                changes.add(name)
                node.value = ast.parse(replacements[name], mode='eval').body
                return node
            return self.generic_visit(node)

    fn = Storage().visit(fn)
    if changes != {'client', 'replay', 'initial', '(_, updated)', 'committed_snapshot'}:
        raise ValueError('upstream trigger feedback persistence boundary changed')
    fn = ast.AsyncFunctionDef(**fn.__dict__)
    ast.fix_missing_locations(fn)
    return ast.unparse(fn) + '\n'


def feedback_sources():
    header = (
        'from __future__ import annotations\n'
        'from datetime import datetime\nfrom dataclasses import dataclass\n'
        'from enum import Enum\nfrom typing import Any,Dict,Mapping,Optional,Tuple\n'
        'import hashlib,json,re\n'
        'from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator\n'
        'from memory_kernel_item import MemoryItem,MemoryItemStatus,MemoryKind\n'
        'from memory_kernel_contracts import LifecycleState\n'
        'from jit_proactivity_models import JIT_CONTENT_FREE_ID_PATTERN\n'
        'from jit_trigger_feedback_models import JITTriggerFeedbackAction,JITTriggerFeedbackReceipt\n'
        'Payload = Dict[str,Any]\n'
    )
    return {
        'jit_trigger_feedback_kernel.py': header
        + selected_nodes(
            'backend/utils/memory/jit_trigger_contract.py',
            {
                'MAX_FEEDBACK_IDS',
                'MAX_FEEDBACK_NOTE_CHARS',
                'TriggerFeedbackAction',
                'TriggerFeedback',
                'FeedbackUpdate',
                'apply_trigger_feedback',
            },
        )
        + selected_nodes(OWNER, {'_LEDGER_SCHEMA_VERSION', 'CanonicalTriggerFeedbackResult'})
        + feedback_policy(),
        'jit_trigger_feedback_wire.py': header
        + selected_nodes('backend/routers/jit_rollout.py', {'JITTriggerFeedbackRequest', 'JITTriggerFeedbackEnvelope'}),
    }
