# MiMo-V2.5-ASR: 可选的 operator-gateway STT(无 GPU)

`backend/utils/mimo_pipeline/` — 通过显式配置的 operator-owned、OpenAI-compatible
gateway 调用 MiMo-V2.5-ASR，服务器无需 GPU。该 provider 是可选能力，不是自托管默认路径。
部署必须自己提供 endpoint、key 和 model authority；仓库不会替部署选择或拼接任何 MiMo
官方云端地址。

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
  │   POST {MIMO_API_BASE}/v1/chat/completions
  │   model: mimo-v2.5-asr
  │   → choices.message.content = 转写文本
  ▼
transcript_callback(text, duration) → 下游(与上游 STT socket 契约一致)
```

## 配置

| env | 默认 | 说明 |
|---|---|---|
| `MIMO_API_KEY` | — | **必需**。operator gateway credential |
| `STT_SERVICE_MODELS` | (上游默认) | 含 `mimo` 即启用本 provider(需 key) |
| `MIMO_API_BASE` | — | **必需**（默认路径）。operator-owned HTTP(S) API 根；官方 MiMo authority 和非法 URL 会 fail-closed |
| `MIMO_TOKENPLAN_BASE` | — | `MIMO_USE_TOKENPLAN=1` 时**必需**；operator-owned TokenPlan-compatible API 根 |
| `MIMO_USE_TOKENPLAN` | `false` | `1`/`true` 选择 `MIMO_TOKENPLAN_BASE`，否则选择 `MIMO_API_BASE` |
| `MIMO_ASR_MODEL` | `mimo-v2.5-asr` | 模型 ID |
| `MIMO_TIMEOUT_SECONDS` | `120` | 请求超时 |

## 边界与说明

- **会话级转写**: MiMo API 是 chat-completions(非 WebSocket),socket 累积整段音频后
  一次性转写——适合会话结束出全文的模式(与 SenseVoice 相同),不是逐帧实时出词。
- **音频上限**: 文档标称 wav/mp3 ≤10MB;超长会话需上游分片。
- **隔离**: 全部 MiMo 特定代码在本目录;上游 touch-point 仅 3 处:
  `utils/stt/streaming.py`(STTService 枚举 + select 分支 + _mimo_available)、
  `routers/listen/receiver.py`(socket 分支)。
- **认证**: `Authorization: Bearer $MIMO_API_KEY`(gateway 可按自身协议转发)。
- **端点安全**: endpoint 必须是无 userinfo/query/fragment 的 HTTP(S) URL；公共 HTTP、metadata/unsafe
  hostname、官方 MiMo 域名都会被拒绝。loopback、容器服务名及私网 HTTP 仅用于 operator-owned 内网。
- **fail-closed**: 缺少 key、所选 endpoint 或 endpoint 非法时，client 构造和 provider availability
  都失败；不会回退到官方云端、其他 key 或默认 provider。
- **说话人**: MiMo ASR 只转写、不分离说话人;分离/识别走 MOSS pipeline 或本地 wespeaker。
