"""Attach shared duplicate admission to the existing Server retry owner."""

from .. import consolidation_transport as owner
from ..registry import Patch


def patches():
    return [
        Patch(
            name='consolidation.' + name,
            module='utils.memory.canonical_consolidation',
            attribute=attribute,
            build=build,
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='retain authoritative identities and reject proven duplicate creates before mutation',
        )
        for name, attribute, build in (
            ('hydrate-identities', 'gather_consolidation_candidates', owner.gather),
            ('validate-duplicates', '_validate_agent_batch', owner.validate),
        )
    ]
