"""Admission and consumer policy for capabilities without a self-host provider."""

from enum import Enum


class Capability(str, Enum):
    STT = 'stt'
    TTS = 'tts'
    PUSH = 'push'
    LLM = 'llm'


class CapabilityDisabled(RuntimeError):
    def __init__(self, capability):
        self.capability = Capability(capability)
        super().__init__(self.capability.value + ' is disabled by the deployment profile')

    def detail(self):
        return {'code': 'deployment_capability_disabled', 'capability': self.capability.value, 'retryable': False}


def validate(row):
    caps = row.get('capabilities', {})
    from .model_contract import validate_speech, validate_llm

    llm = validate_llm(row.get('llm'))
    if caps.get('llm_provider', 'disabled') != (llm.provider if llm else 'disabled'):
        raise ValueError('LLM capability must match the selected model')

    speech = validate_speech(row.get('speech'))
    if caps.get('push_provider') != 'disabled':
        raise ValueError('self-host push has no admitted provider')
    if caps.get('stt_providers') != (['sensevoice'] if speech else []) or caps.get('tts_provider') != (
        'kokoro' if speech else 'disabled'
    ):
        raise ValueError('speech capabilities must match the admitted model bundle')


def reject(capability):
    raise CapabilityDisabled(capability)


def push_not_delivered(*args, **kwargs):
    # Preserve the count contract used by best-effort reminders after Tasks are
    # committed. Zero is never a delivery receipt; the public send API refuses.
    from utils.observability.fallback import record_fallback

    record_fallback(
        component='pusher', from_mode='send', to_mode='disabled', reason='dispatch_disabled', outcome='degraded'
    )
    return 0


async def push_not_delivered_async(*args, **kwargs):
    return push_not_delivered(*args, **kwargs)


def push_notification_not_delivered(*args, **kwargs):
    """Void notification callers get no delivery claim or cooldown side effect."""
    push_not_delivered()
