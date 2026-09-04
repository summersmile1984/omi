"""Admission and consumer policy for capabilities without a self-host provider."""

from enum import Enum


class Capability(str, Enum):
    STT = 'stt'
    TTS = 'tts'
    PUSH = 'push'


class CapabilityDisabled(RuntimeError):
    def __init__(self, capability):
        self.capability = Capability(capability)
        super().__init__(self.capability.value + ' is disabled by the deployment profile')

    def detail(self):
        return {'code': 'deployment_capability_disabled', 'capability': self.capability.value, 'retryable': False}


def validate(row):
    caps = row.get('capabilities', {})
    if (
        caps.get('stt_providers') != []
        or caps.get('tts_provider') != 'disabled'
        or caps.get('push_provider') != 'disabled'
    ):
        raise ValueError(
            'self-host STT/TTS/push have no admitted provider; enable them with a verified provider contract'
        )


def reject(capability):
    raise CapabilityDisabled(capability)


def push_not_delivered(*args, **kwargs):
    # Preserve the count contract used by best-effort reminders after Tasks are
    # committed. Zero is never a delivery receipt; the public send API refuses.
    from utils.observability.fallback import record_fallback

    record_fallback(
        component='push', from_mode='send', to_mode='disabled', reason='dispatch_disabled', outcome='degraded'
    )
    return 0


async def push_not_delivered_async(*args, **kwargs):
    return push_not_delivered(*args, **kwargs)
