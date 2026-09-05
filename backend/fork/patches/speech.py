"""Bind all existing STT consumers to one admitted model and provider selection."""

from ..registry import Patch


def patches():
    from .. import speech

    def prerecorded(original):
        from utils.sensevoice.prerecorded_provider import SenseVoicePrerecordedProvider

        def provider(language='en'):
            speech.language(language)
            if speech.streaming_service() == 'mimo':
                from ..mimo_speech import prerecorded

                return prerecorded()
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

    def listen_receiver(original):
        class ServingListenReceiver(original):
            async def _create_stt_socket(self, callback, sample_rate, modulate_callback=None):
                if self.host.stt_service == speech.streaming_service():
                    return speech.new_socket(
                        sample_rate=sample_rate, transcript_callback=callback, language=self.host.stt_language
                    )
                return await super()._create_stt_socket(callback, sample_rate, modulate_callback)

        return ServingListenReceiver

    def listen_provider(original):
        def provider(service):
            if service == speech.streaming_service():
                return speech.streaming_service()
            return original(service)

        return provider

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
        ('routers.listen.receiver', 'provider_for_service', listen_provider),
        ('routers.listen.receiver', 'ListenReceiver', listen_receiver),
        ('routers.listen.runtime', 'ListenReceiver', listen_receiver),
        ('utils.sensevoice.socket', 'get_sensevoice_recognizer', lambda original: speech.recognizer),
        ('utils.sensevoice.prerecorded_provider', 'get_sensevoice_recognizer', lambda original: speech.recognizer),
        ('utils.sensevoice.socket', 'SenseVoiceSocket', socket),
        ('utils.stt.outcomes', '_KNOWN_PROVIDERS', lambda original: original | {'sensevoice', 'mimo'}),
    ]
    selected_patches = [
        Patch(
            'speech.' + module + '.' + attribute,
            module,
            attribute,
            build,
            lambda row, module=module: row.get('target') == 'self_hosted'
            and not (row.get('operator_ai') and module.startswith('utils.sensevoice.')),
            'one admitted local model owns both canonical and captured speech consumers',
        )
        for module, attribute, build in targets
    ]
    from ..mimo_listen import runtime

    selected_patches.append(
        Patch(
            'speech.mimo.listen-audio-owner',
            'routers.listen.runtime',
            'ListenSessionRuntime',
            runtime,
            lambda row: row.get('target') == 'self_hosted' and bool(row.get('operator_ai')),
            'normal disconnect drains accepted ASR windows and their transcript owner before finalization',
        )
    )
    return selected_patches
