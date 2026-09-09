# Local CPU speech runtime

The selected self-host profile pairs SenseVoice Small int8 (2024-07-17) with
Kokoro multilingual v1.0, using the already locked sherpa-onnx 1.13.4 Linux amd64
runtime. The target profile owns both archive SHA-256 values, the complete
unpacked inventory hash and the existing Silero VAD artifact hash. API admission
verifies every model file, initializes all three models, and executes real TTS
and ASR before importing the serving app. Missing files, altered contents,
wrong runtime and conflicting independent speech environment settings stop the
process. A worker neither loads speech models nor requires their mount.

Provision outside the repository, with Python 3.11 or newer:

```bash
python3.12 deploy/self-host/prepare-speech.py --output /srv/omi/models/speech
```

The provisioner downloads the two fixed public release archives, verifies their
hashes before bounded extraction, verifies the complete resulting tree, then
publishes it. Existing output is verified without overwrite. Keep the selected
bundle immutable. Set `SPEECH_MODEL_STORE=/srv/omi/models/speech` in the private
Compose env file and build using `build-images.sh`; Compose mounts it read-only
at `/models/speech`. No request or runtime startup downloads models. A bundle
change is a reviewed profile change and a fresh provisioned directory.

The verified unpacked bundle is 641,292,524 bytes / 387 files. Native inference
has been exercised in a Linux amd64 container restricted to two CPUs and 2 GiB,
with networking disabled. Those limits describe the isolated speech probe;
size the complete API, embedding service and databases separately. Native ASR
and TTS each use two CPU threads and repository executors. One process-wide ASR
lock serializes decoding; concurrent TTS returns `speech_busy` instead of
building an unbounded inference queue. Launch one API process per container.

Existing authenticated routes are retained:

| Route | Selected behavior |
|---|---|
| `POST /v2/voice-message/transcribe` | Existing PCM and multipart request decoding, admission and result envelope; local ASR provider |
| `WS /v2/voice-message/transcribe-stream` | PCM16 mono, transcript segment arrays, `finalize`, then clean close only after a successful drain |
| `POST /v1/tts/synthesize` | Local Kokoro synthesis; default MP3, explicit `wav` available |
| `POST /v2/tts/synthesize` | Local Kokoro synthesis with MP3 response for the desktop wire contract |
| `/v4/listen`, `/v4/web/listen` | Existing ambient orchestration selects the same local adapter; full conversation/LLM workflow requires separate verification |

ASR admits en/zh/ja/ko/yue and automatic language detection, up to 60 seconds per
prerecorded/PTT request. PCM accepts 8–48 kHz; HTTP stereo is downmixed, WS is
mono only. Encoded input and URL downloads are capped at 8 MiB; FFmpeg decoding
has a 20-second timeout, permits the input pipe only and rejects decoded audio over
60 seconds. URL authority must pass the shared egress policy; redirects are
refused. Custom keywords and explicitly requested multi-speaker output are
unsupported. Voice identification remains disabled, and all segments carry the
single-speaker label. Segment timestamps are window bounds, not word alignment.

PTT applies the same admitted Silero VAD before decoding each window; genuine
silence skips ASR, while VAD errors fail finalization. It emits independent
windows of at most five seconds, retains no more than
15 seconds of pending PCM, and has a 30-second audio idle timeout. Empty audio
and non-finalize text frames do not renew that deadline. A failed
native pump makes finalization fail with `stt_failed` and WS 1011; provider
admission failure closes 1013. It never silently selects a remote provider.
Existing rate/budget dependencies remain enforced. Inference success does not
imply a persisted conversation or LLM response.

One terminal drain records accepted PCM duration on finalize, disconnect, idle,
limit or cancellation. Input rejected before admission is excluded, and a
provider rejection or failed drain is not charged. Cancelling a waiter cannot
cancel the accepted tail or repeat its usage write. This retains the existing
Redis quota owner's failure policy; it does not introduce durable billing.

TTS admits 500 characters, with `af_heart` (English) and `zf_xiaobei` (Chinese).
Already-shipped default voice requests select the deployment's default based on
text; arbitrary vendor voices, model settings and instructions are refused.
Output is checked for finite, nonempty, nonzero mono samples and bounded to
60 seconds. Invalid input is HTTP 400; unavailable/inference/encoding errors
are HTTP 503 with explicit code and retryability. Redis rate-limit unavailability
fails closed. Default output is actual 44.1-kHz / 128-kbps MP3 encoded by FFmpeg.

Removing the optional `speech` profile object derives `stt_providers: []` and
`tts_provider: disabled`; HTTP/WS/background callers explicitly refuse speech.
Do not set capability fields independently. Push remains disabled, with actual
zero deliveries for background reminders and explicit public registration/send
refusals. No voice model enables external network egress.

Model provenance: the official [SenseVoice instructions](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)
and [Kokoro v1.0 instructions](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html)
define these archives and voice IDs. The SenseVoice model card and included
Kokoro model LICENSE identify Apache-2.0. The bundle also contains espeak-ng
phonemizer data; its upstream [COPYING](https://github.com/espeak-ng/espeak-ng/blob/master/COPYING)
is GPL-3.0. Preserve component notices and source provenance when distributing;
the model's license is not a blanket license for all runtime dependencies.

Hermetic tests execute through the existing `fork-selfhost-startup` local/CI
lane. Real recorded English/Chinese audio, synthesis and actual deployment wire
results are listed independently in the dated SH3 speech verification document.
