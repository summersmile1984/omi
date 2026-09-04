"""Bind all existing STT consumers to one admitted model and provider selection."""

from ..registry import Patch


def patches():
    from .. import speech

    def prerecorded(original):
        from utils.sensevoice.prerecorded_provider import SenseVoicePrerecordedProvider

        def provider(language='en'):
            speech.language(language)
            return SenseVoicePrerecordedProvider(speech.recognizer())

        return provider

    def socket(original):
        from utils.stt.vad import linear16_pcm_is_silent

        class ServingSocket(original):
            def __init__(self, *args, **kwargs):
                kwargs['recognizer'] = speech.recognizer()
                kwargs['silence_detector'] = linear16_pcm_is_silent
                super().__init__(*args, **kwargs)
                self.start()

        return ServingSocket

    targets = [
        ('utils.stt.pre_recorded', 'get_prerecorded_service', lambda original: speech.prerecorded_selection),
        ('utils.chat', 'get_prerecorded_service', lambda original: speech.prerecorded_selection),
        ('routers.chat', 'get_prerecorded_service', lambda original: speech.prerecorded_selection),
        ('utils.sync.pipeline', 'get_prerecorded_service', lambda original: speech.prerecorded_selection),
        ('utils.stt.pre_recorded', 'get_prerecorded_provider', prerecorded),
        ('utils.stt.streaming', 'get_stt_service_for_language', lambda original: speech.streaming_selection),
        ('routers.chat', 'get_stt_service_for_language', lambda original: speech.streaming_selection),
        ('routers.listen.runtime', 'get_stt_service_for_language', lambda original: speech.streaming_selection),
        ('routers.listen.receiver', 'get_stt_service_for_language', lambda original: speech.streaming_selection),
        ('utils.sensevoice.socket', 'get_sensevoice_recognizer', lambda original: speech.recognizer),
        ('utils.sensevoice.prerecorded_provider', 'get_sensevoice_recognizer', lambda original: speech.recognizer),
        ('utils.sensevoice.socket', 'SenseVoiceSocket', socket),
        ('utils.stt.outcomes', '_KNOWN_PROVIDERS', lambda original: original | {'sensevoice'}),
    ]
    return [
        Patch(
            'speech.' + module + '.' + attribute,
            module,
            attribute,
            build,
            lambda row: row.get('target') == 'self_hosted',
            'one admitted local model owns both canonical and captured speech consumers',
        )
        for module, attribute, build in targets
    ]
