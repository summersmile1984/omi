"""Route existing deletion database seams to the self-hosted authority owner."""

from functools import wraps

from .. import account_deletion as owner
from ..registry import Patch


def _wrap(action):
    def build(original):
        @wraps(original)
        def call(*args, **kwargs):
            return action(original, *args, **kwargs)

        return call

    return build


def patches():
    direct = {
        'delete_user_data': owner.delete_user_data,
        'get_user_deletion_wipe_status': owner.get_status,
        'mark_user_deletion_wipe_completed': owner.complete,
        'record_late_agent_vm_cleanup': owner.late_cleanup,
        'mark_user_deletion_wipe_intent': owner.intent,
        'mark_user_deletion_billing_failed': owner.billing_failed,
    }
    wrapped = {
        'resolve_deletion_wipe_job_id': owner.resolve_job,
        'resolve_legacy_deletion_wipe_uid': owner.resolve_legacy,
        'mark_user_deletion_wipe_running': owner.guard_mutation,
        'mark_user_deletion_wipe_failed': owner.guard_mutation,
        'set_user_deletion_feedback': owner.guard_mutation,
        'cancel_user_deletion_wipe': owner.guard_mutation,
    }
    return [
        Patch(
            name=f'account-deletion.{name}',
            module='database.users',
            attribute=name,
            build=(lambda original, replacement=action: replacement) if name in direct else _wrap(action),
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='PG erasure and minimal receipts retain one deletion authority across retries',
        )
        for name, action in {**direct, **wrapped}.items()
    ]
