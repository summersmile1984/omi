# MOSS Pipeline: 转写 + 说话人分离 + 说话人识别（无 GPU）

本目录用显式配置的 MOSS-compatible operator endpoint 打通转写、说话人分离和
说话人识别。仓库不内置 api.mosi.cn 或其他 vendor authority；未配置 endpoint
时 fail-closed。

## 两种 transport

| transport | wire | credential |
|---|---|---|
| mosi | OpenMOSS /v1/files + /v1/audio/transcriptions file/task API | MOSS_API_KEY 必填 |
| mlx_audio | OpenAI-compatible multipart /v1/audio/transcriptions | operator 可不设 key |

两种 wire 由不同 adapter 实现，不会把 mlx-audio 伪装成 MOSS file/task API。
MossSpeakerPipeline 仍输出下游兼容的匿名 S01/S02 说话人标签，识别步骤
使用现有 embedding matcher 回填 person。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| MOSS_TRANSPORT | mosi | mosi 或 mlx_audio |
| MOSS_API_KEY | — | mosi 必填；mlx_audio 可留空 |
| MOSS_API_BASE | — | 必填 operator HTTP(S) authority；不允许 vendor 默认值 |
| MOSS_MODEL | moss-transcribe-diarize（仅 mosi） | mlx_audio 必须显式设置服务端模型 ID |
| MOSS_TIMEOUT_SECONDS | 120 | 请求超时 |
| MOSS_AUDIO_URL_ALLOWLIST | — | caller 音频 URL 若指向 localhost/私网，必须显式允许 host |
| HOSTED_SPEAKER_EMBEDDING_API_URL | — | 现有 diarizer /v2/embedding 端点 |

## 本机 mlx-audio

当前本机服务真实 wire 是 OpenAI-compatible multipart，不是 MOSS /v1/files。
先用 /v1/models 读取服务端模型 ID，再配置 adapter：

    curl -fsS http://127.0.0.1:5002/v1/models
    export STT_PRERECORDED_MODEL=moss
    export MOSS_TRANSPORT=mlx_audio
    export MOSS_API_BASE=http://127.0.0.1:5002
    export MOSS_MODEL=kuotient/MOSS-Transcribe-Diarize-MLX-8bit

adapter 调用 POST /v1/audio/transcriptions，以 multipart file、model、
response_format=verbose_json 发送音频；不会调用或伪造 /v1/files。本机
127.0.0.1:5002 仅作为显式 operator endpoint 使用，不会被默认探测或替换。

## Egress 与 fail-closed

MOSS_API_BASE 和 caller 提供的音频 URL 在 transport 前都会校验：

- api.mosi.cn 及其子域、metadata/link-local/保留地址、userinfo 均拒绝；
- 公网 endpoint/audio URL 必须 HTTPS；
- 私有音频 authority 只有在 MOSS_AUDIO_URL_ALLOWLIST 明确允许时可下载；
- HTTP client 禁用 redirect，避免校验后的请求跳转到另一 authority；
- 显式选择 STT_PRERECORDED_MODEL=moss 但缺 endpoint/key/model 时直接报配置错误，
  不会静默回落到 managed provider。

## Pipeline 用法

    from utils.moss_pipeline.pipeline import MossSpeakerPipeline

    pipe = MossSpeakerPipeline()
    wav = open("meeting.wav", "rb").read()
    result = pipe.run(wav, people_embeddings, transcribe_model="moss-transcribe-diarize")

MOSS 只提供匿名说话人分离，不提供 person identification；后者由本地或既有
embedding service 完成。
