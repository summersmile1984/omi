"""Project the upstream reservation policy, replacing only storage boundaries."""

import ast

from screen_frame_sources import selected_nodes, source

OWNER = 'backend/database/jit_proactivity_store.py'


def trigger_compiler():
    path = 'backend/utils/memory/jit_trigger_contract.py'
    tree = ast.parse(source(path))
    definitions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node
    names = {'DEFAULT_TRIGGER_RUNTIME_POLICY', 'CompiledTrigger', 'TriggerAction', 'compile_memory_item_trigger'}
    pending = list(names)
    while pending:
        node = definitions[pending.pop()]
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id in definitions and child.id not in names:
                names.add(child.id)
                pending.append(child.id)
    return selected_nodes(path, names)


def reservation_policy():
    tree = ast.parse(source(OWNER))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_reserve_transaction')
    fn.decorator_list = []
    fn.args.args = [ast.arg(arg='store'), ast.arg(arg='proposed', annotation=ast.Name(id='JITProactivityEventReceipt'))]
    reads = {
        'user_snapshot': "store.profile()",
        'control_snapshot': "store.control()",
        'event_snapshot': "store.record('jit_events', proposed.event_id)",
        'parent_snapshot': "store.record('jit_events', proposed.parent_event_id)",
        'trigger_snapshot': 'store.trigger(proposed.trigger_memory_id)',
        'budget_control_snapshot': "store.record('jit_budget_controls', 'current')",
        'day_snapshot': "store.record('jit_budgets', proposed.budget_day)",
        'candidate_snapshot': "store.record('jit_turns', proposed.candidate_id)",
    }
    keys = {
        'event_ref': "('jit_events', proposed.event_id)",
        'budget_control_ref': "('jit_budget_controls', 'current')",
        'day_ref': "('jit_budgets', proposed.budget_day)",
        'candidate_ref': "('jit_turns', proposed.candidate_id)",
    }
    # The existing Candidate transaction owns the account deletion/generation
    # fence both before reading and inside its atomic commit, including replay.
    removed = {'collections', 'deletion_ref', 'deletion_snapshot', 'deletion_payload', 'deletion_status'}
    seen = set()

    class Storage(ast.NodeTransformer):
        def visit_Assign(self, node):
            name = node.targets[0].id if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None
            if name in removed:
                seen.add(name)
                return None
            if name in reads:
                seen.add(name)
                node.value = ast.Await(ast.parse(reads[name], mode='eval').body)
                return node
            if name in keys:
                seen.add(name)
                node.value = ast.parse(keys[name], mode='eval').body
                return node
            return self.generic_visit(node)

        def visit_If(self, node):
            if ast.unparse(node.test) == 'account_deletion_blocks_access(deletion_status)':
                seen.add('deletion_fence')
                return None
            return self.generic_visit(node)

        def visit_Call(self, node):
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == 'getattr'
                and len(node.args) >= 2
                and ast.literal_eval(node.args[1]) == 'exists'
            ):
                return ast.Compare(self.visit(node.args[0]), [ast.IsNot()], [ast.Constant(None)])
            if isinstance(node.func, ast.Name) and node.func.id == 'parse_snapshot_strict':
                return ast.Call(ast.Name(id='_parse'), [self.visit(arg) for arg in node.args], [])
            if ast.unparse(node.func) == 'transaction.set':
                return ast.Call(
                    ast.Attribute(ast.Name(id='store'), 'put'),
                    [ast.Starred(self.visit(node.args[0])), self.visit(node.args[1])],
                    [],
                )
            return self.generic_visit(node)

    fn = Storage().visit(fn)
    if seen != set(reads) | set(keys) | removed | {'deletion_fence'}:
        raise ValueError('upstream JIT reservation storage boundary changed')
    fn = ast.AsyncFunctionDef(**fn.__dict__)
    fn.name = 'reserve_policy'
    ast.fix_missing_locations(fn)
    return ast.unparse(fn) + '\n'


def proposal_policy():
    tree = ast.parse(source(OWNER))
    fn = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'reserve_jit_proactivity_event'
    )
    fn.name = 'propose_receipt'
    fn.args.kwonlyargs[-1].arg = 'timezone_name'
    fn.args.kw_defaults[-1] = None
    body = []
    for node in fn.body:
        name = node.targets[0].id if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) else None
        if name in {'client', 'transaction'}:
            continue
        if name == 'normalized_timezone':
            node.value = ast.Name(id='timezone_name')
        if name == 'proposed':
            node.value = ast.Call(ast.Name(id='_parse'), node.value.args, [])
        if isinstance(node, ast.Return):
            node.value = ast.Name(id='proposed')
        body.append(node)
    fn.body = body
    ast.fix_missing_locations(fn)
    return ast.unparse(fn) + '\n'


def jit_sources():
    header = (
        'from __future__ import annotations\n'
        'from datetime import datetime,time,timedelta,timezone\n'
        'from dataclasses import dataclass\nfrom enum import Enum\n'
        'from typing import Any,Dict,List,Mapping,Optional,Pattern,Sequence,Tuple\n'
        'from zoneinfo import ZoneInfo,ZoneInfoNotFoundError\n'
        'import hashlib,json,re\n'
        'from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator\n'
        'from memory_kernel_item import MemoryItem,MemoryItemStatus,MemoryKind,MemorySubjectScope\n'
        'from memory_kernel_evidence import SourceState\n'
        'from memory_kernel_apply import MemoryControlState\n'
        'from jit_proactivity_models import *\n'
    )
    authority = selected_nodes(
        'backend/utils/memory/jit_trigger_snapshot.py',
        {
            'AuthoritativeTriggerComponents',
            '_authoritative_trigger_components',
            'is_authoritative_trigger_for_paid_work',
        },
    )
    helpers = selected_nodes(
        OWNER,
        {
            'JITProactivityReservationError',
            '_budget_day_for_timezone',
            '_next_local_midnight',
            '_timezone_from_user_snapshot',
        },
    )
    parser = '''
class JITMalformedAuthority(RuntimeError):
    pass

def _snapshot_payload(snapshot):
    if not isinstance(snapshot, dict):
        raise JITProactivityReservationError('required JIT authority document is unavailable')
    return snapshot

def _parse(model, payload):
    try:
        return model.model_validate(payload)
    except Exception as error:
        raise JITMalformedAuthority('JIT authority record is malformed') from error

'''
    return {
        'jit_proactivity_kernel.py': header
        + trigger_compiler()
        + authority
        + helpers
        + parser
        + reservation_policy()
        + proposal_policy(),
        'jit_proactivity_wire.py': header
        + selected_nodes(
            'backend/routers/jit_rollout.py', {'JITProactivityReservationRequest', 'JITProactivityReservationEnvelope'}
        ),
    }
