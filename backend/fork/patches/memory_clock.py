"""Keep canonical operation transition time monotonic in Server deployments."""

from ..memory_operation_clock import install_operation_clock
from ..registry import Patch


def patches():
    return [
        Patch(
            name='canonical-memory.operation-clock',
            module='models.memory_operations',
            attribute='MemoryOperation',
            build=install_operation_clock,
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='a runtime wall-clock regression must not invalidate a canonical operation transition',
        )
    ]
