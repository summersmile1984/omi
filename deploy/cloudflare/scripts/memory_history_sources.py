"""Stage upstream history admission, wire projection and bounded page policy."""

import ast

from screen_frame_sources import selected_nodes, source

MODELS = 'backend/models/memories.py'
SERVICE = 'backend/utils/memory/memory_service.py'
ADAPTER = 'backend/utils/memory/canonical_memory_adapter.py'
BUDGET = 'backend/utils/other/list_budget.py'


def schema_class(name, omitted):
    tree = ast.parse(selected_nodes(MODELS, {name}))
    node = tree.body[0]
    found = {child.name for child in node.body if isinstance(child, ast.FunctionDef) and child.name in omitted}
    if found != omitted:
        raise ValueError('upstream memory schema construction helpers changed')
    node.body = [child for child in node.body if not isinstance(child, ast.FunctionDef) or child.name not in omitted]
    return ast.unparse(tree) + '\n'


def page_policy():
    cls = next(
        node
        for node in ast.parse(source(SERVICE)).body
        if isinstance(node, ast.ClassDef) and node.name == 'MemoryService'
    )
    fn = next(
        node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'read_ledger_history_page'
    )
    fn.args.args[0].arg = 'store'
    changes = set()

    class Storage(ast.NodeTransformer):
        def visit_For(self, node):
            if (
                isinstance(node.iter, ast.Call)
                and ast.unparse(node.iter.func) == 'iter_authoritative_product_memory_items_newest_first'
            ):
                changes.add('iterator')
                node.iter = ast.parse('store.items(scan_limit, budget)', mode='eval').body
                return ast.AsyncFor(**node.__dict__)
            return self.generic_visit(node)

        def visit_Call(self, node):
            if ast.unparse(node.func) == 'self._is_ledger_history_item':
                changes.add('policy')
                node.func = ast.Name(id='is_ledger_history_item', ctx=ast.Load())
            return self.generic_visit(node)

    # Transform the nested policy call before replacing its enclosing loop.
    fn = Storage().visit(fn)
    fn = Storage().visit(fn)
    if changes != {'iterator', 'policy'}:
        raise ValueError('upstream history page boundary changed')
    fn = ast.AsyncFunctionDef(**fn.__dict__)
    ast.fix_missing_locations(fn)
    return ast.unparse(fn) + '\n'


def history_sources():
    header = (
        'from __future__ import annotations\n'
        'import copy,json,os,time\nfrom enum import Enum\nfrom dataclasses import dataclass\n'
        'from datetime import datetime,timezone\nfrom typing import Any,Callable,Dict,Iterable,List,Mapping,Optional,Sequence,Tuple,cast\n'
        'from fastapi import HTTPException\nfrom pydantic import BaseModel,Field,computed_field,field_validator\n'
        'from memory_kernel_item import LedgerWriteReason,MemoryItem,MemoryItemStatus,MemoryKind,MemorySubjectScope,MemoryTier,ProcessingState,SourceState,RESTRICTED_SENSITIVITY_LABELS,MAX_MEMORY_ARGUMENTS_JSON_BYTES\n'
        'from memory_kernel_domain import tier_to_layer\nPayload=Dict[str,Any]\n'
    )
    wire = header + selected_nodes(MODELS, {'MemoryCategory', 'SubjectAttribution'})
    wire += schema_class('Memory', {'get_memories_as_str', 'render'})
    wire += schema_class('Evidence', {'from_source'})
    wire += schema_class('MemoryDB', {'calculate_score', 'from_memory'})
    wire += selected_nodes(
        'backend/utils/memory/belief_model.py',
        {
            'MEMORY_BELIEF_MODEL_ENABLED_ENV',
            'HALF_LIFE_DAYS_BY_CLASS',
            'CURRENT_BAND_MIN',
            'FADING_BAND_MIN',
            'CurrencyBand',
            'BeliefView',
            'belief_model_enabled',
            '_coerce_aware_utc',
            'resolve_last_evidenced_at',
            'derive_half_life_days',
            'compute_currency',
            'currency_band',
            'belief_view',
            '_enum_value',
            '_record_category',
            'belief_view_for_record',
            'public_belief_overlay',
        },
    )
    wire += selected_nodes(ADAPTER, {'_payload_or_empty', '_bounded_memory_arguments', 'memory_item_to_memorydb'})
    wire += selected_nodes(
        'backend/utils/memory/memory_api_contract.py',
        {
            'MemoryApiExposure',
            'CANONICAL_LIFECYCLE_FIELDS',
            'BELIEF_VIEW_FIELDS',
            'MEMORY_INTERNAL_FIELDS',
            '_payload',
            'memory_api_payload',
            'memory_api_payloads',
        },
    )
    budget = ast.parse(selected_nodes(BUDGET, {'ListReadBudget'}))
    budget.body[0].body = [
        node for node in budget.body[0].body if not isinstance(node, ast.FunctionDef) or node.name != 'observe'
    ]
    rules = header + 'from memory_history_wire import MemoryDB,memory_item_to_memorydb\n'
    rules += (
        selected_nodes(
            BUDGET,
            {
                'OMI_LIST_TRUNCATED_HEADER',
                'OMI_LIST_TRUNCATED_VALUE',
                'REQUEST_STARTED_MONOTONIC_STATE_KEY',
                'LIST_READ_SERIALIZATION_HEADROOM_SECONDS',
                'LIST_READ_DEFAULT_BUDGET_SECONDS',
                'LIST_READ_DEFAULT_MAX_DOCUMENTS',
                'LIST_READ_MIN_RPC_SECONDS',
                'BudgetExhaustionReason',
                '_env_float',
                '_env_int',
                'resolve_list_read_budget_seconds',
                'resolve_list_read_max_documents',
                'ListReadBudgetExhausted',
            },
        )
        + ast.unparse(budget)
        + '\n'
    )
    rules += selected_nodes('backend/utils/memory/knowledge_ledger.py', {'LEDGER_SCHEMA_VERSION'})
    rules += selected_nodes('backend/utils/memory/ledger_history_policy.py', {'is_ledger_history_item'})
    rules += selected_nodes(SERVICE, {'LedgerHistoryPage', 'MAX_LEDGER_HISTORY_PROVIDER_WINDOW'})
    cls = next(
        node
        for node in ast.parse(source(SERVICE)).body
        if isinstance(node, ast.ClassDef) and node.name == 'HistoricalMemoryAdapter'
    )
    limit = next(
        node
        for node in cls.body
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == 'MAX_COMPATIBILITY_WINDOW'
    )
    rules += 'class HistoricalMemoryAdapter:\n    ' + ast.unparse(limit) + '\n' + page_policy()
    return {'memory_history_wire.py': wire, 'memory_history_kernel.py': rules}
