# 本地 SenseVoice-Small 流式 STT(CPU,无 GPU、无 API)

`backend/utils/sensevoice/` — live 听路径的本地说话人转写。

## 为什么用 SenseVoice 做 live

- **MOSS 是批处理**:即使 SSE 也是"整段音频上传后流式返回文本",不适合实时 live。
- **SenseVoice-Small(234M)** 本地 CPU 推理:中文 CER **7.81%**,CPU **17.2x 实时**,单条 0.04s。
- 免费、离线、无 API 依赖;中文质量远超 whisper.cpp tiny(中文不可用)。

## 依赖与模型

```bash
uv pip install sherpa-onnx   # CPU 推理运行时(已装 1.13.4)

# 下载 SenseVoice ONNX 模型(约 1GB,含 int8)
curl -L -o /tmp/sensevoice.tar.bz2 \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
tar xjf /tmp/sensevoice.tar.bz2
```

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SENSEVOICE_MODEL_DIR` | `/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17` | 模型目录(含 model.int8.onnx + tokens.txt) |
| `SENSEVOICE_NUM_THREADS` | `4` | CPU 线程数 |
| `SENSEVOICE_USE_ITN` | `1` | 逆文本正则化(数字/标点) |
| `STT_SERVICE_MODELS` | `modulate-velma-2,dg-nova-3,parakeet` | 加 `sensevoice` 启用 |

## 启用 live 用 SenseVoice

```bash
export SENSEVOICE_MODEL_DIR="/path/to/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
export STT_SERVICE_MODELS="sensevoice"   # 或 "modulate-velma-2,sensevoice,parakeet"
```

选择逻辑(`get_stt_service_for_language`):`STT_SERVICE_MODELS` 含 `sensevoice` 且模型目录存在 → 选 `STTService.sensevoice`。

## 实现

- `socket.py` — `SenseVoiceSocket` 实现上游 `STTSocket` 契约:
  - `send()` 累积 PCM16
  - `finish()` 触发 SenseVoice(sherpa-onnx CPU)整段转写,经回调返回
  - `finalize()` / `is_connection_dead` / `death_reason` 满足契约
- 懒加载进程级 recognizer(`get_sensevoice_recognizer`,线程安全)
- 上游改动: `utils/stt/streaming.py` 加 `STTService.sensevoice` 枚举 + 选择分支;`routers/listen/receiver.py` 加 socket 构建分支(自包含)

## 验证(2026-08-09, Mac arm64 CPU)

- 中文 say 音频 → SenseVoiceSocket → "今天天气很好，我们去公园散步。"(0.04s)
- 上游 115 个 streaming/policy 测试全过(SenseVoice 未配置时行为不变)

## 说明

- SenseVoice 是**非流式**模型:累积整段后在 `finish()` 一次性转写。对 live 场景是"准实时"(按音频结束输出),满足 Omi 的录音后转写语义。
- 如需真正的增量流式,VAD 门控(`GatedSTTSocket`/`vad_gate`)可先切语音段再逐段送 SenseVoice——同一 socket 契约。
