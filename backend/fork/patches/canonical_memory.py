"""Attach mutation identity repair to both upstream Server entrypoints."""

from ..canonical_mutations import include_patch_arguments
from ..registry import Patch


def patches():
    return [
        Patch(
            name='canonical-memory.' + attribute,
            module='utils.memory.canonical_memory_adapter',
            attribute=attribute,
            build=include_patch_arguments,
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='hash changed arguments before the canonical operation and patch enter the apply kernel',
        )
        for attribute in ('_apply_canonical_user_mutation', 'apply_canonical_user_mutation')
    ]
