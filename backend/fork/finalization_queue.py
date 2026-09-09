"""Redis transport for the existing PG finalization outbox and fenced job lease."""

from .profile import ProfileError
from .queue_config import QUEUES

FINALIZATION = next(queue for queue in QUEUES if queue.name == 'finalization')


def configured():
    try:
        FINALIZATION.validate()
    except ProfileError:
        return False
    return True


def enabled():
    # Installed only for the selected Redis target. An unavailable transport
    # retains the durable outbox; it must never switch to inline processing.
    return True
