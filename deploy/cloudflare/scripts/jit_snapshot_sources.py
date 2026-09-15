"""Stage the original exhaustive watchlist with only D1 IO relocated."""

import ast

from screen_frame_sources import selected_nodes, source

OWNER = 'backend/utils/memory/jit_trigger_snapshot.py'
WIRE = 'backend/routers/jit_rollout.py'


def reader():
    tree = ast.parse(source(OWNER))
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'read_authoritative_trigger_snapshot'
    )
    fn.args.kwonlyargs = [ast.arg(arg='store')]
    fn.args.kw_defaults = [None]
    changes = {'client': 0, 'collection': 0, 'head': 0, 'snapshots': 0, 'payload': 0, 'id': 0, 'budget': 0}

    class Storage(ast.NodeTransformer):
        def visit_Assign(self, node):
            name = node.targets[0].id if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None
            if name in {'client', 'collection'}:
                changes[name] += 1
                return None
            if name == 'snapshots':
                changes[name] += 1
                node.value = ast.parse('await store.triggers(MAX_AUTHORITATIVE_TRIGGERS + 1)', mode='eval').body
                return node
            if name == 'payload':
                changes[name] += 1
                node.value = ast.parse("snapshot['item']", mode='eval').body
                return node
            return self.generic_visit(node)

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == 'read_memory_v3_trusted_account_generation':
                changes['head'] += 1
                return ast.parse('await store.head()', mode='eval').body
            if isinstance(node.func, ast.Name) and node.func.id == '_budget_authority':
                changes['budget'] += 1
                return ast.parse(f'await _budget_authority({ast.unparse(node.args[1])}, store)', mode='eval').body
            return self.generic_visit(node)

        def visit_Attribute(self, node):
            if ast.unparse(node) == 'snapshot.id':
                changes['id'] += 1
                return ast.parse("snapshot['id']", mode='eval').body
            return self.generic_visit(node)

    fn = Storage().visit(fn)
    if changes != {
        'client': 1,
        'collection': 1,
        'head': 3,
        'snapshots': 1,
        'payload': 1,
        'id': 1,
        'budget': 2,
    }:
        raise ValueError('upstream JIT snapshot storage boundary changed')
    fn = ast.AsyncFunctionDef(**fn.__dict__)
    ast.fix_missing_locations(fn)
    return ast.unparse(fn) + '\n'


def budget_authority():
    """Relocate the profile-timezone read; the projection itself stays upstream's.

    Upstream resolves the pacing window by reading `users/{uid}.time_zone` through a
    Firestore client.  D1 owns that profile field here, so only the read is swapped for
    `store.timezone()`; every validation and the returned `(local day, timezone name)`
    tuple stay the original statements, and the assertion below fails loudly if upstream
    changes that shape instead of silently emitting a kernel that projects the wrong day.
    """

    tree = ast.parse(source(OWNER))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_budget_authority')
    changes = {'document': 0, 'payload': 0}

    class Profile(ast.NodeTransformer):
        def visit_Assign(self, node):
            name = node.targets[0].id if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None
            if name == 'snapshot':
                if '.document(' not in ast.unparse(node.value):
                    raise ValueError('upstream JIT budget authority no longer reads one profile document')
                changes['document'] += 1
                return None
            if name == 'payload':
                changes['payload'] += 1
                node.value = ast.parse("{'time_zone': await store.timezone()}", mode='eval').body
                return node
            return self.generic_visit(node)

    fn = Profile().visit(fn)
    if changes != {'document': 1, 'payload': 1}:
        raise ValueError('upstream JIT budget authority storage boundary changed')
    blocks = [node for node in fn.body if isinstance(node, ast.Try)]
    if len(blocks) != 1:
        raise ValueError('upstream JIT budget authority storage boundary changed')
    block = blocks[0]
    projected = ast.unparse(ast.parse('return at.astimezone(zone).date().isoformat(), timezone_name').body[0])
    if ast.unparse(block.body[-1]) != projected:
        raise ValueError('upstream JIT budget authority projection changed')
    if ast.unparse(block.handlers[-1].body[-1]) != 'return None':
        raise ValueError('upstream JIT budget authority no longer fails open')
    fn.args = ast.arguments(
        posonlyargs=[],
        args=[ast.arg(arg='at'), ast.arg(arg='store')],
        vararg=None,
        kwonlyargs=[],
        kw_defaults=[],
        kwarg=None,
        defaults=[],
    )
    fn.returns = None
    fn = ast.AsyncFunctionDef(**fn.__dict__)
    ast.fix_missing_locations(fn)
    return ast.unparse(fn) + '\n'


def snapshot_wire():
    tree = ast.parse(source(WIRE))
    fn = next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'get_jit_trigger_snapshot'
    )
    final = fn.body[-1]
    if not isinstance(final, ast.Return) or ast.unparse(final.value.func) != 'JITTriggerSnapshotEnvelope':
        raise ValueError('upstream JIT snapshot response boundary changed')
    result = ast.FunctionDef(
        name='snapshot_envelope',
        args=ast.arguments(posonlyargs=[], args=[ast.arg(arg='snapshot')], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[final],
        decorator_list=[],
    )
    ast.fix_missing_locations(result)
    return ast.unparse(result) + '\n'


def snapshot_sources():
    header = (
        'from __future__ import annotations\n'
        'from dataclasses import dataclass\nfrom datetime import datetime,timezone\n'
        'from enum import Enum\nfrom typing import Any\nimport hashlib,json\n'
        'from zoneinfo import ZoneInfo\n'
        'from pydantic import BaseModel,ConfigDict,Field\n'
        'from memory_kernel_item import MemoryItem,MemoryItemStatus,MemoryKind\n'
        'from jit_proactivity_kernel import (CompiledTrigger,TriggerAction,TriggerRuntimePolicy,'
        'DEFAULT_TRIGGER_RUNTIME_POLICY,_authoritative_trigger_components)\n'
    )
    return {
        'jit_trigger_snapshot_kernel.py': header
        + selected_nodes(
            'backend/utils/memory/v3/account_generation_source.py',
            {
                'V3AccountGenerationFailureReason',
                'V3TrustedAccountGenerationReadError',
                'V3TrustedAccountGenerationResult',
            },
        )
        + selected_nodes(
            OWNER,
            {
                'MAX_AUTHORITATIVE_TRIGGERS',
                'AuthoritativeTriggerRow',
                'AuthoritativeTriggerSnapshot',
                '_revision',
                '_empty_watchlist_revision',
            },
        )
        + budget_authority()
        + reader(),
        'jit_trigger_snapshot_wire.py': header
        + selected_nodes(
            WIRE,
            {
                'JITTriggerActionEnvelope',
                'JITTriggerSnapshotRowEnvelope',
                'JITTriggerSnapshotEnvelope',
                '_disabled_trigger_snapshot',
            },
        )
        + snapshot_wire(),
    }
