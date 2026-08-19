"""MiMo-V2.5-ASR integration for the cloud-neutral Omi fork.

Xiaomi MiMo-V2.5-ASR is an OpenAI-compatible speech recognition API (chat
completions with ``input_audio`` base64 parts). It handles Mandarin, English,
Chinese dialects, code-switching, lyrics, and noisy/multi-speaker audio on
Xiaomi's side — no local GPU needed. Chinese-quality first-tier at a fraction
of Western ASR pricing (TokenPlan ~0.285 CNY/h).

Role split in this fork:
  - **STT (live streaming)** → MiMo-V2.5-ASR via ``socket.MimoSttSocket``
  - **ASR (pre-recorded batch)** → OpenMOSS via ``utils.moss_pipeline``

Modules:
  - ``mimo_client``  thin HTTP client for the ASR endpoint
  - ``socket``       upstream STTSocket implementation (live streaming)

Opt-in via ``STT_SERVICE_MODELS=mimo`` + ``MIMO_API_KEY``.
"""
