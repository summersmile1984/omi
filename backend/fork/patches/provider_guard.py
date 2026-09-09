"""Declare both canonical and captured upstream write-fence imports explicitly."""

from .. import provider_guard as owner
from ..registry import Patch

# Registry verifies all targets before mutation. These existing `from ... import`
# aliases have already captured the original function by then, so each consumer
# is a named, verified seam rather than an implicit sys.modules sweep.
CONSUMERS = (
    'database.legal_holds',
    'database.memories',
    'database.vector_db',
    'utils.memory.atom_keyword_index',
    'utils.other.storage',
)


def patches():
    selected = lambda row: row.get('target') == 'self_hosted'
    entries = [
        Patch(
            name=f'provider.receipt-fence.{module}',
            module=module,
            attribute='external_write_fence',
            build=owner.fence,
            applies_to=selected,
            reason='completed receipts and in-flight writes must share the wipe authority',
        )
        for module in CONSUMERS
    ]
    entries += [
        Patch(
            name='provider.minio-write-gate',
            module='utils.other.storage',
            attribute='owner_storage_write_gate',
            build=lambda original: owner.storage_gate,
            applies_to=selected,
            reason='self-host MinIO/local stage contains real data and cannot use the upstream fake-store bypass',
        ),
        Patch(
            name='provider.wipe-lock',
            module='services.users.account_deletion',
            attribute='background_wipe_user_data',
            build=owner.wipe,
            applies_to=selected,
            reason='wait for admitted provider writes before inspecting/purging the account; busy jobs retry',
        ),
    ]
    for name, module, attribute, build in (
        ('completion-proof', 'database.users', 'mark_user_deletion_wipe_completed', owner.complete),
        ('purge-closure', 'services.users.account_deletion', 'purge_derived_user_data', owner.purge),
    ):
        entries.append(
            Patch(
                name='provider.' + name,
                module=module,
                attribute=attribute,
                build=build,
                applies_to=selected,
                reason='provider absence is required before removing the recoverable deletion marker',
            )
        )
    from .. import provider_objects
    from ..pg_write_policy import TerminalReceiptWrites

    entries.append(
        Patch(
            name='provider.pg-terminal-write',
            module='firestore_pg.write_policy',
            attribute='policy',
            build=lambda original: TerminalReceiptWrites(),
            applies_to=selected,
            reason='late SQL writers and provider calls must observe the same completed deletion authority',
        )
    )

    for module in ('utils.other.storage', 'services.users.account_deletion'):
        entries.append(
            Patch(
                name='provider.minio-purge.' + module,
                module=module,
                attribute='delete_all_user_storage_objects',
                build=lambda original: provider_objects.purge_owner,
                applies_to=selected,
                reason='local self-host storage is real and must sweep owned prefixes',
            )
        )
    entries.append(
        Patch(
            name='provider.minio-signed-cache',
            module='utils.other.storage',
            attribute='_get_signed_url',
            build=lambda original: provider_objects.signed_url,
            applies_to=selected,
            reason='signed URL identity includes public origin, bucket and object',
        )
    )
    return entries
