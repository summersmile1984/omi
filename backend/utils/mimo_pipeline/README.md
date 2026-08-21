# MiMo-V2.5-ASR: 实时 STT(无 GPU)

`backend/utils/mimo_pipeline/` — 用显式配置的 OpenAI-compatible MiMo-V2.5-ASR authority
做**实时(流式)STT**,服务器无需 GPU。中文质量第一梯队(普通话+英语+方言+语码混合+歌词+嘈杂/多说话人),
价格低(TokenPlan 等效 ~0.285 元/h)。

角色分工(本 fork):
- **STT(实时流式)** → MiMo-V2.5-ASR(`socket.MimoSttSocket`)
- **ASR(预录制批处理)** → OpenMOSS(`utils/moss_pipeline/`)

## 链路

```
PCM16 音频 (16kHz, 单声道)
  │
  │ ① send() 累积到 bytearray(与 SenseVoiceSocket 同模式)
  ▼
finish() — 会话结束
  │
  │ ② PCM16 → WAV 容器 (wave 模块)
  │   base64 → input_audio content part
  │   POST {configured operator base}/v1/chat/completions
  │   model: mimo-v2.5-asr
  │   → choices.message.content = 转写文本
  ▼
transcript_callback(text, duration) → 下游(与上游 STT socket 契约一致)
```

## 配置

| env | 默认 | 说明 |
|---|---|---|
| `MIMO_API_KEY` | — | **必需**。小米 MiMo 开放平台 key |
| `STT_SERVICE_MODELS` | (上游默认) | 含 `mimo` 即启用本 provider(需 key) |
| `MIMO_API_BASE` | — | 必须显式配置的 operator/self-host API 根；没有 vendor 默认 |
| `MIMO_TOKENPLAN_BASE` | — | 仅在显式选择 TokenPlan 时使用的 operator-approved API 根 |
| `MIMO_USE_TOKENPLAN` | — | `1`/`true` 时改用已显式配置的 `MIMO_TOKENPLAN_BASE` |
| `MIMO_ASR_MODEL` | `mimo-v2.5-asr` | 模型 ID |
| `MIMO_TIMEOUT_SECONDS` | `120` | 请求超时 |

## 边界与说明

- **会话级转写**: MiMo API 是 chat-completions(非 WebSocket),socket 累积整段音频后
  一次性转写——适合会话结束出全文的模式(与 SenseVoice 相同),不是逐帧实时出词。
- **音频上限**: 文档标称 wav/mp3 ≤10MB;超长会话需上游分片。
- **隔离**: 全部 MiMo 特定代码在本目录;上游 touch-point 仅 3 处:
  `utils/stt/streaming.py`(STTService 枚举 + select 分支 + _mimo_available)、
  `routers/listen/receiver.py`(socket 分支)。
- **认证**: `Authorization: Bearer $MIMO_API_KEY`。endpoint、key 必须在 client 构造前同时通过校验；缺失或带 query/userinfo 的 URL 会 fail-closed。
- **说话人**: MiMo ASR 只转写、不分离说话人;分离/识别走 MOSS pipeline 或本地 wespeaker。
