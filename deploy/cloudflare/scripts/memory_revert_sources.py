"""Project upstream explicit history restoration; replace only its IO boundaries."""

import ast
from screen_frame_sources import selected_nodes, source

SERVICE = 'backend/utils/memory/memory_service.py'
LEDGER = 'backend/utils/memory/knowledge_ledger.py'
ADAPTER = 'backend/utils/memory/canonical_memory_adapter.py'


def asynchronous(node, replacements, *, cache=False):
    class IO(ast.NodeTransformer):
        def visit_Expr(self, value):
            # Core reads its canonical head and has no server process prompt cache.
            if (
                cache
                and isinstance(value.value, ast.Call)
                and ast.unparse(value.value.func) == 'self._invalidate_prompt_cache'
            ):
                return None
            return self.generic_visit(value)

        def visit_Call(self, value):
            name = ast.unparse(value.func)
            value = self.generic_visit(value)
            if name in replacements:
                value.func = ast.parse(replacements[name], mode='eval').body
                return ast.Await(value=value)
            return value

    node = IO().visit(node)
    node = ast.AsyncFunctionDef(**node.__dict__)
    ast.fix_missing_locations(node)
    return node


def revert_sources():
    header = '''from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Mapping, Tuple, cast
from uuid import UUID
from pydantic import BaseModel, Field, field_validator, model_validator
from fastapi import HTTPException
from memory_kernel_item import (MemoryItem, MemoryItemStatus, MemoryKind, MemoryLayer, MemorySubjectScope, LedgerWriteReason, ProcessingState, SourceState, RESTRICTED_SENSITIVITY_LABELS, MAX_LEDGER_PLAYBOOK_BODY_CHARACTERS, MAX_LEDGER_TRIGGER_CONDITION_CHARACTERS, MAX_LEDGER_TRIGGER_CONDITION_KEYS)
from memory_kernel_evidence import MemoryEvidence, ArtifactRef, ArtifactPreservationState
from memory_kernel_contracts import deterministic_contract_id, DurablePatchDecision, LifecycleState
from memory_kernel_operations import MemoryOperation, MemoryOperationType, MemoryLedgerReopenReceipt
from memory_kernel_apply import MemoryControlState, build_patch_mutation_identity
from memory_kernel_intake import _product_metadata_from_payload, SubjectAttribution
from memory_history_wire import MemoryDB, memory_item_to_memorydb, _payload_or_empty, belief_model_enabled, HALF_LIFE_DAYS_BY_CLASS
from memory_ledger_policy import canonicalize_ledger_slot, normalize_playbook_handle, PLAYBOOK_HANDLE_CHARACTER_LIMIT
Payload = Dict[str, Any]
'''
    names = {
        'LEDGER_SCHEMA_VERSION',
        'MAX_PLAYBOOK_BODY_CHARACTERS',
        'MAX_TRIGGER_CONDITION_KEYS',
        '_DIRECT_USER_AMEND_AUTHORITY',
        'LedgerProvenance',
        'LedgerWrite',
        '_row_id',
        '_evidence_id',
        'evidence_id_for_ledger_provenance',
    }
    code = header + selected_nodes(LEDGER, names)
    # Preserve the original semantic builders. Their db_client is the typed D1
    # append owner; it is never a public input or a Firestore compatibility shim.
    for node in ast.parse(
        selected_nodes(LEDGER, {'save_ledger_write', 'amend_user_fact', 'reopen_standalone_fact'})
    ).body:
        mapping = {
            'save_ledger_write': 'save_ledger_write',
            'write_memory': 'write_memory',
            'ensure_canonical_apply_control_state': 'db_client.control',
            'read_canonical_memory_item': 'db_client.active_item',
        }
        # Both references retain the upstream private authority selection; the
        # ordinary ledger writer has no Cloudflare binding in this user surface.
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id == 'write_canonical_direct_user_knowledge_ledger_memory':
                child.id = 'direct_user_write'
        code += ast.unparse(asynchronous(node, mapping)) + '\n'
    code += '''async def direct_user_write(uid, data, *, db_client, **kwargs):
    return await db_client.append(uid, data, **kwargs)

def write_canonical_knowledge_ledger_memory(*args, **kwargs):
    raise ValueError('this surface requires explicit user append authority')
'''
    code += selected_nodes(SERVICE, {'LedgerRevertIdentity', 'MAX_LEDGER_REVERT_CHAIN_LENGTH'})
    owner = next(
        n for n in ast.parse(source(SERVICE)).body if isinstance(n, ast.ClassDef) and n.name == 'MemoryService'
    )
    methods = {
        '_normalized_revert_operation_id',
        '_ledger_revert_identity',
        '_validate_ledger_revert_item',
        '_is_standalone_closed_ledger_fact',
        '_is_exact_standalone_ledger_reopen',
        '_is_exact_ledger_fact_revert',
        'reopen_standalone_closed_ledger_fact',
        'revert_superseded_ledger_fact',
    }
    owner.body = [n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    if {n.name for n in owner.body} != methods:
        raise ValueError('upstream revert service boundary changed')
    for index, node in enumerate(owner.body):
        if node.name in {'reopen_standalone_closed_ledger_fact', 'revert_superseded_ledger_fact'}:
            owner.body[index] = asynchronous(
                node,
                {
                    'self.ensure_canonical_mutation_ready': 'self.ensure_canonical_mutation_ready',
                    'self._canonical_item_for_lineage': 'self._canonical_item_for_lineage',
                    'self.reopen_standalone_closed_ledger_fact': 'self.reopen_standalone_closed_ledger_fact',
                    'reopen_standalone_fact': 'reopen_standalone_fact',
                    'amend_fact': 'amend_user_fact',
                },
                cache=True,
            )
    ast.fix_missing_locations(owner)
    code += ast.unparse(owner) + '\n'
    code += selected_nodes('backend/database/memory_apply_store.py', {'CanonicalApplyWrite'})
    code += selected_nodes('backend/utils/memory/belief_model.py', {'SUBJECT_SCOPE_ALIASES', 'horizon_from_extraction'})
    code += selected_nodes(
        ADAPTER,
        {
            '_ALLOWED_MEMORY_VISIBILITIES',
            '_ordered_capture_devices_from_evidence',
            '_legacy_evidence_to_memory',
            '_resolve_initial_tier_value',
            '_visibility_from_payload',
            '_user_asserted_from_payload',
            '_relationship_to_user_from_payload',
            '_canonical_extraction_apply_write',
        },
    )
    # _canonical_extraction_apply_write receives explicit ledger identity, scope,
    # and evidence from append(); extraction-only branches are not reachable.
    return {
        'memory_revert_kernel.py': code,
        'memory_ledger_policy.py': source('backend/models/knowledge_ledger_policy.py'),
    }
