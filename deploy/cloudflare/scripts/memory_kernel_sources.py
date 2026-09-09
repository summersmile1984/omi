#!/usr/bin/env python3
"""Stage canonical memory rules with fork-owned runtime clock dependencies."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from screen_frame_sources import selected_nodes
from memory_consolidation_sources import consolidation_sources
from candidate_kernel_sources import candidate_sources
from recommendation_sources import recommendation_sources
from recurrence_sources import recurrence_sources
from jit_proactivity_sources import jit_sources
from jit_snapshot_sources import snapshot_sources
from jit_feedback_sources import feedback_sources
from memory_history_sources import history_sources
from memory_revert_sources import revert_sources
from jit_ledger_snapshot_sources import ledger_snapshot_sources

ROOT = Path(__file__).resolve().parents[3]
MODULES = {
    'models.jit_trigger_feedback': 'jit_trigger_feedback_models',
    'models.jit_proactivity': 'jit_proactivity_models',
    'utils.task_intelligence.rollout': 'candidate_kernel_rollout',
    'utils.durable_queue_policy': 'integration_queue_policy',
    'models.memory_apply': 'memory_kernel_apply',
    'models.memory_admission': 'memory_kernel_admission',
    'models.memory_contracts': 'memory_kernel_contracts',
    'models.memory_promotion': 'memory_kernel_promotion',
    'models.memory_domain': 'memory_kernel_domain',
    'models.memory_operations': 'memory_kernel_operations',
    'models.product_memory': 'memory_kernel_item',
    'models.memory_evidence': 'memory_kernel_evidence',
    'models.memory_review': 'memory_kernel_review',
    'models.memory_recurrence': 'memory_kernel_recurrence',
    'models.candidate': 'candidate_kernel_models',
    'models.task_recommendation': 'candidate_kernel_recommendation',
    'models.goal': 'candidate_kernel_goal',
    'models.workstream': 'candidate_kernel_workstream',
    'models.workstream_association': 'candidate_kernel_association',
    'models.action_item': 'memory_kernel_action_item',
    'models.task_intelligence': 'memory_kernel_task_intelligence',
    'utils.memory.required_promotion': 'memory_kernel_required_promotion',
    'utils.log_sanitizer': 'memory_kernel_sanitizer',
    'utils.memory.short_term_lifecycle': 'memory_kernel_short_term_lifecycle',
    'utils.memory.canonical_lineage': 'memory_kernel_lineage',
}


def project(module: str) -> str:
    path = ROOT / 'backend' / (module.replace('.', '/') + '.py')
    if not path.is_file() or path.is_symlink():
        raise ValueError('memory kernel source must be an ordinary repository file')
    content = path.read_text()
    lines = content.splitlines(keepends=True)
    replacements = []
    for node in ast.walk(ast.parse(content)):
        if isinstance(node, ast.ImportFrom) and node.module:
            dependency = node.module
            if dependency.startswith(('models.', 'utils.')):
                if node.level or dependency not in MODULES:
                    raise ValueError('memory kernel dependency changed; review the source boundary')
                replacements.append((node.lineno - 1, node.col_offset, dependency))
        elif isinstance(node, ast.Import):
            if any(alias.name.startswith(('models.', 'utils.')) for alias in node.names):
                raise ValueError('memory kernel package import requires an explicit projection')
    for line, column, dependency in sorted(replacements, reverse=True):
        before, after = 'from ' + dependency + ' import ', 'from ' + MODULES[dependency] + ' import '
        if not lines[line][column:].startswith(before):
            raise ValueError('memory kernel import syntax changed')
        lines[line] = lines[line][:column] + after + lines[line][column + len(before) :]
    return ''.join(lines)


def generate(output: Path) -> None:
    outputs = {target + '.py': project(module) for module, target in MODULES.items()}
    outputs['memory_kernel_operation_clock.py'] = (ROOT / 'backend/fork/memory_operation_clock.py').read_text()
    outputs['memory_kernel_operations.py'] += (
        '\nfrom memory_kernel_operation_clock import install_operation_clock\n'
        'MemoryOperation = install_operation_clock(MemoryOperation)\n'
    )
    outputs.update(consolidation_sources())
    outputs.update(candidate_sources())
    outputs.update(recommendation_sources())
    outputs.update(recurrence_sources())
    outputs.update(jit_sources())
    outputs.update(snapshot_sources())
    outputs.update(feedback_sources())
    outputs.update(history_sources())
    outputs.update(revert_sources())
    outputs.update(ledger_snapshot_sources())
    outputs['integration_kernel.py'] = (
        'import hashlib\nimport json\nfrom typing import Any,Dict,List,Optional,Tuple\n'
        'from integration_queue_policy import QueuePolicy\n'
        + selected_nodes('backend/database/candidate_integration_outbox.py', {'CANDIDATE_INTEGRATION_POLICY'})
        + selected_nodes('backend/utils/notifications.py', {'_generate_tag', '_build_apple_reminders_sync_message'})
    )
    outputs['memory_kernel_intake.py'] = (
        'import hashlib,uuid\nfrom enum import Enum\nfrom typing import Any, Dict\n'
        + selected_nodes('backend/database/document_ids.py', {'document_id_from_seed'})
        + selected_nodes('backend/models/memories.py', {'SubjectAttribution'})
        + selected_nodes('backend/utils/memory/canonical_memory_adapter.py', {'_product_metadata_from_payload'})
    )
    outputs['memory_kernel_privacy.py'] = (
        'from __future__ import annotations\n'
        'from datetime import datetime\nfrom typing import List\n'
        'from memory_kernel_apply import MemoryControlState, MemoryOutboxEvent, MemoryOutboxEventType\n'
        'from memory_kernel_contracts import deterministic_contract_id\n'
        'from memory_kernel_evidence import (MemoryEvidence, ArtifactPreservationState, SourceState, '
        'SourceStateReason, ProvenanceVisibility, RedactionStatus)\n'
        'from memory_kernel_item import MemoryItem, MemoryItemStatus, MemoryKind, MemorySubjectScope\n\n'
        + selected_nodes(
            'backend/database/memory_apply_store.py',
            {'_privacy_tombstoned_evidence', '_privacy_tombstoned_memory_item', '_privacy_delete_events'},
        )
    )
    for name, content in outputs.items():
        compile(content, name, 'exec')
        if (output / name).exists() or (output / name).is_symlink():
            raise ValueError('memory kernel projection collides with another source owner')
    for name, content in outputs.items():
        (output / name).write_text(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    generate(parser.parse_args().output)
