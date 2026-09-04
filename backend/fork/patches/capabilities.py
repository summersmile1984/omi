"""Block provider selectors and the shared push send boundary before routers serve."""

from ..capabilities import Capability, push_not_delivered, push_not_delivered_async, reject
from ..registry import Patch


def patches():
    def stt(original):
        def unavailable(*args, **kwargs):
            return reject(Capability.STT)

        return unavailable

    targets = [
        ('utils.stt.pre_recorded', 'get_prerecorded_service', stt),
        ('utils.stt.streaming', 'get_stt_service_for_language', stt),
        ('routers.chat', 'get_prerecorded_service', stt),
        ('utils.sync.pipeline', 'get_prerecorded_service', stt),
        ('routers.chat', 'get_stt_service_for_language', stt),
        ('routers.listen.runtime', 'get_stt_service_for_language', stt),
        ('routers.listen.receiver', 'get_stt_service_for_language', stt),
        ('utils.notifications', '_send_to_user', lambda original: push_not_delivered),
        ('utils.notifications', '_send_to_user_async', lambda original: push_not_delivered_async),
        ('utils.notifications', '_send_messages', lambda original: lambda *args, **kwargs: reject(Capability.PUSH)),
    ]
    targets.extend(
        ('utils.stt.pre_recorded', name, stt)
        for name in (
            'deepgram_prerecorded',
            'deepgram_prerecorded_from_bytes',
            'modulate_prerecorded',
            'modulate_prerecorded_from_bytes',
            'parakeet_prerecorded',
            'parakeet_prerecorded_from_bytes',
        )
    )
    return [
        Patch(
            name='capability.' + module + '.' + attribute,
            module=module,
            attribute=attribute,
            build=build,
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='disabled profile capabilities must not call upstream vendor defaults',
        )
        for module, attribute, build in targets
    ]
