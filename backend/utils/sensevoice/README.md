# Local SenseVoice adapter

The self-host target selects this adapter through `fork/patches/speech.py`.
`deploy/profiles/self_hosted.yaml` and `fork/model_contract.py` own the exact
SenseVoice Small int8 archive, sherpa-onnx runtime and bundle identity. Configure
and provision it through [the deployment guide](../../../deploy/self-host/speech-runtime.md).
Do not install unpinned wheels or use independent model-path environment defaults.

`socket.py` implements the existing `STTSocket` contract using independent
5-second PCM windows; SenseVoice is an offline recognizer, not a stateful
streaming model. PCM16 samples are converted to normalized floats. Native decode
runs in the shared executor behind one process-wide decode lock. `finalize()`
flushes a VAD boundary; `drain_and_close()` waits for the tail and raises if the
pump failed. The selected deployment injects the admitted Silero VAD before
window decoding so silent tails cannot produce hallucinated words; VAD errors
remain failures. Odd frames and more than 15 seconds of pending PCM are rejected
before buffer growth. No failed drain is reported as successful transcription.

`prerecorded_provider.py` accepts bounded PCM or encoded audio. FFmpeg decoding
allows the input pipe protocol only, is time-bounded and caps decoded duration at
60 seconds. URL downloads enforce the shared egress owner, reject redirects and
cap compressed input at 8 MiB. The current target admits a single speaker only;
custom keywords and explicitly requested multiple speakers are unsupported.
ASR timestamps cover decoded windows rather than word-aligned timings.

`speaker.py` retains its separately tested window-clustering implementation,
but the current self-host profile explicitly disables speaker embeddings and
selects `single_speaker`. It is not evidence of speaker identity or diarization.

Hermetic behavior is in `backend/fork/tests/test_speech_contract.py` and
`test_speech_transport.py`, selected by the existing startup local/CI lane.
Actual licensed-model CPU measurements, recordings and current wire-test status
are recorded separately in the dated SH3 speech verification document. Do not
carry forward older accuracy/speed claims or treat synthesized audio alone as
recorded-speech recognition evidence.
