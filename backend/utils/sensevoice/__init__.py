"""Local SenseVoice-Small streaming (CPU, no GPU, no API).

Live listen path drop-in: SenseVoice-Small via sherpa-onnx on CPU
(CER 7.81% on Chinese, 17.2x realtime). Keeps MOSS for batch/diarized
transcription while live audio stays local.

See socket.py for the STTSocket implementation and README.md for setup.
"""
