"""Stage upstream ledger snapshot validation, mirror cursors and page decisions."""

import ast
from screen_frame_sources import selected_nodes, source

MIRROR = 'backend/utils/memory/jit_ledger_mirror_snapshot.py'
WIRE = 'backend/routers/jit_ledger_snapshot.py'
MIGRATION = 'backend/utils/memory/knowledge_ledger_migration.py'


def mirror_reader():
    fn = next(
        n
        for n in ast.parse(source(MIRROR)).body
        if isinstance(n, ast.FunctionDef) and n.name == 'read_authoritative_ledger_mirror_page'
    )
    fn.args.kwonlyargs[-1].arg = 'store'
    changes = set()

    class IO(ast.NodeTransformer):
        def visit_Assign(self, node):
            name = ast.unparse(node.targets[0])
            if name in {'collection', 'query'}:
                changes.add(name)
                return None
            if name == 'snapshots' and isinstance(node.value, ast.Call):
                changes.add('snapshots')
                node.value = ast.parse('await store.rows(cursor_state.last_memory_id, page_size + 1)', mode='eval').body
                return node
            if name == 'payload':
                changes.add('payload')
                node.value = ast.parse("snapshot['item']", mode='eval').body
                return node
            return self.generic_visit(node)

        def visit_If(self, node):
            if ast.unparse(node.test) == 'cursor_state.last_memory_id is not None':
                changes.add('keyset')
                return None
            return self.generic_visit(node)

        def visit_Call(self, node):
            if ast.unparse(node.func) == 'cursor_secret':
                changes.add('secret')
                return ast.parse('store.secret()', mode='eval').body
            if ast.unparse(node.func) == '_read_fence':
                changes.add('fence')
                return ast.parse('await store.fence()', mode='eval').body
            return self.generic_visit(node)

        def visit_Attribute(self, node):
            if ast.unparse(node) in {'snapshot.id', 'snapshots[-1].id'}:
                changes.add('id')
                return ast.Subscript(value=self.visit(node.value), slice=ast.Constant(value='id'), ctx=ast.Load())
            return self.generic_visit(node)

    fn = IO().visit(fn)
    if changes != {'collection', 'query', 'snapshots', 'payload', 'keyset', 'secret', 'fence', 'id'}:
        raise ValueError('upstream mirror storage boundary changed')
    fn = ast.AsyncFunctionDef(**fn.__dict__)
    ast.fix_missing_locations(fn)
    return ast.unparse(fn) + '\n'


def prompt_reader():
    fn = next(
        n
        for n in ast.parse(source(WIRE)).body
        if isinstance(n, ast.FunctionDef) and n.name == '_build_enabled_snapshot'
    )
    fn.args.kwonlyargs[0].arg = 'store'

    class IO(ast.NodeTransformer):
        def visit_Call(self, node):
            name = ast.unparse(node.func)
            if name == 'read_ledger_migration_completion':
                return ast.parse('await store.completion()', mode='eval').body
            if name == 'read_ledger_prompt_projection_receipt':
                return ast.parse('await store.projection(completion)', mode='eval').body
            return self.generic_visit(node)

    fn = ast.AsyncFunctionDef(**IO().visit(fn).__dict__)
    ast.fix_missing_locations(fn)
    return ast.unparse(fn) + '\n'


def mirror_envelope():
    fn = next(
        n
        for n in ast.parse(source(WIRE)).body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == 'get_knowledge_ledger_mirror_snapshot'
    )
    # Reuse the complete original fence/row/alias envelope after rollout checks.
    start = next(
        i for i, n in enumerate(fn.body) if isinstance(n, ast.If) and ast.unparse(n.test) == 'page.fence is None'
    )
    value = ast.FunctionDef(
        name='mirror_envelope',
        args=ast.arguments(
            posonlyargs=[], args=[ast.arg(arg='uid'), ast.arg(arg='page')], kwonlyargs=[], kw_defaults=[], defaults=[]
        ),
        body=fn.body[start:],
        decorator_list=[],
    )
    ast.fix_missing_locations(value)
    return ast.unparse(value) + '\n'


def fence_reader():
    fn = next(n for n in ast.parse(source(MIRROR)).body if isinstance(n, ast.FunctionDef) and n.name == '_read_fence')
    fn.args.kwonlyargs[0].arg = 'store'

    class IO(ast.NodeTransformer):
        def visit_Call(self, node):
            replacements = {
                'read_ledger_migration_completion': 'await store.completion()',
                'read_ledger_prompt_projection_receipt': 'await store.projection(completion, validate_rows=False)',
                'read_memory_v3_trusted_account_generation': 'await store.trusted()',
            }
            name = ast.unparse(node.func)
            if name in replacements:
                return ast.parse(replacements[name], mode='eval').body
            return self.generic_visit(node)

    fn = ast.AsyncFunctionDef(**IO().visit(fn).__dict__)
    ast.fix_missing_locations(fn)
    return ast.unparse(fn) + '\n'


def ledger_snapshot_sources():
    header = '''from __future__ import annotations
import base64,hashlib,hmac,json,time
from dataclasses import dataclass
from datetime import datetime,timezone
from enum import Enum
from typing import Any,Literal,Optional
from pydantic import BaseModel,ConfigDict,Field
from memory_kernel_apply import MemoryControlState,WriterMode
from memory_kernel_item import MemoryItem,MemoryItemStatus,MemoryKind,MemorySubjectScope,memory_item_has_lifecycle_metadata
from memory_kernel_evidence import ArtifactPreservationState,ProvenanceVisibility,RedactionStatus,SourceState
from memory_history_wire import MemoryDB,memory_item_to_memorydb
from memory_ledger_policy import select_profile_slot_winners
from jit_policy import TriState
'''
    code = header + selected_nodes('backend/utils/memory/knowledge_ledger.py', {'LEDGER_SCHEMA_VERSION'})
    code += selected_nodes('backend/utils/memory/universal_list_cursor.py', {'UniversalListCursorError'})
    code += selected_nodes(
        MIGRATION,
        {
            'LedgerMigrationCompletion',
            'LedgerPromptProjectionReceipt',
            'MAX_LEDGER_PROMPT_PROJECTION_ROWS',
            'LedgerMigrationPublicationError',
            '_same_control_fence',
            '_prompt_eligible',
            '_bounded_prompt_projection',
        },
    )
    code += selected_nodes(
        MIRROR,
        {
            'MIRROR_SCHEMA_VERSION',
            'DEFAULT_MIRROR_PAGE_SIZE',
            'MAX_MIRROR_PAGE_SIZE',
            'MAX_MIRROR_CURSOR_CHARS',
            'MIRROR_CURSOR_PREFIX',
            'MIRROR_CURSOR_TTL_SECONDS',
            'LedgerMirrorFence',
            'LedgerMirrorAlias',
            'LedgerMirrorRow',
            'LedgerMirrorPage',
            'LedgerMirrorCursor',
            '_b64encode',
            '_cursor_signature',
            '_encode_cursor',
            '_decode_cursor',
            '_aliases',
            '_is_content_free_privacy_tombstone',
            '_project_row',
            '_page_revision',
            '_next_chain_revision',
            '_failure',
        },
    )
    code += fence_reader() + mirror_reader()
    code += selected_nodes(
        WIRE,
        {
            'LedgerPromptSnapshotMode',
            'LedgerPromptSnapshotEnvelope',
            'LedgerMirrorAliasEnvelope',
            'LedgerMirrorRowEnvelope',
            'LedgerMirrorSnapshotEnvelope',
            '_disabled_mirror_snapshot',
            '_disabled_snapshot',
        },
    )
    code += prompt_reader() + mirror_envelope()
    return {'jit_ledger_snapshot_kernel.py': code}
