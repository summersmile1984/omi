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
| `SENSEVOICE_STREAM_WINDOW_SECONDS` | `5.0` | 无 VAD 边界时的最长增量窗口 |
| `SENSEVOICE_SPEAKER_MODE` | `auto` | `auto` 在 speaker provider 为 `sherpa_onnx` 时启用本地窗口聚类；也可显式选择 `single_speaker` / `window_clustering` |
| `SENSEVOICE_SPEAKER_WINDOW_SECONDS` | `5.0` | 预录音频的 ASR + speaker embedding 分段窗口(0.5–30 秒) |
| `SENSEVOICE_SPEAKER_CLUSTER_THRESHOLD` | `0.45` | speaker embedding 余弦距离阈值(0–2) |
| `SENSEVOICE_SPEAKER_MAX_CLUSTERS` | `8` | 单会话最多 speaker 数(1–32)；达到上限后不创建新 id，有 telemetry 地归入最近既有 cluster |
| `SENSEVOICE_SPEAKER_MAX_AUDIO_SECONDS` | `21600` | 预录 diarization 最大音频长度，最高允许 86400 秒 |
| `SPEAKER_EMBEDDING_PROVIDER` | `http` | self-host 设为 `sherpa_onnx` 才会启用无网络 speaker 聚类 |
| `SPEAKER_EMBEDDING_MODEL` | 无 | `sherpa_onnx` speaker embedding ONNX 的显式只读挂载路径；运行时绝不下载 |
| `STT_SERVICE_MODELS` | `dg-nova-3,modulate-velma-2,parakeet` | 设为 `sensevoice` 启用本地实时路径 |

## 启用 live 用 SenseVoice

```bash
export SENSEVOICE_MODEL_DIR="/path/to/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
export STT_SERVICE_MODELS="sensevoice"   # 或 "modulate-velma-2,sensevoice,parakeet"
```

选择逻辑(`get_stt_service_for_language`):`STT_SERVICE_MODELS` 含 `sensevoice`，且 `model.int8.onnx` 与 `tokens.txt` 都存在时，才会选择 `STTService.sensevoice`。运行镜像锁定 `sherpa-onnx`，缺 wheel/模型会在会话 ready 前失败。

## 实现

- `socket.py` — `SenseVoiceSocket` 实现上游 `STTSocket` 契约:
  - `send()` 累积 PCM16，并由后台 pump 按 5 秒窗口持续解码
  - `finalize()` 在 VAD 语音边界强制刷新，不关闭会话
  - `finish()` / `drain_and_close()` 等待尾段解码完成
  - CPU 推理由共享 `sync_executor` 承载，不阻塞 WebSocket event loop
- `speaker.py` — 使用显式挂载的 sherpa speaker embedding 给每个 ASR/VAD 窗口做有界在线聚类:
  - speaker id 按首次出现稳定分配为 `SPEAKER_00`、`SPEAKER_01`…；错序、坏向量、模型缺失均 typed fail-closed
  - 达到 speaker 上限后不会静默创建第 N+1 个 id，也不会中断 Capture：返回最近既有 id、保持 centroid 不受低置信样本污染，并记录共享 fallback telemetry
  - 仅允许 `sherpa_onnx`，不会借用 `SPEAKER_EMBEDDING_PROVIDER=http`，不会构造 HTTP 请求或隐式下载模型
  - live 解码与 embedding 都经共享 `sync_executor`；finalize/drain 仍逐个消费有界窗口，不会把积压会话合成一次无界推理
- `prerecorded_provider.py` — `diarize=true` 且本地 speaker provider 已选择时，按同一窗口契约逐段 ASR、聚类并返回带时间戳的多 speaker segments；oversize/错误配置返回 typed transcription failure
- 预录 `transcribe_url` 下载前会经过统一 neutral/self-host egress policy；中立部署只能下载内部 Compose authority 或 `SELF_HOST_EGRESS_ALLOWLIST` 中的 operator-owned origin，未声明的 caller URL fail-closed
- 懒加载进程级 recognizer(`get_sensevoice_recognizer`,线程安全)
- 上游改动: `utils/stt/streaming.py` 加 `STTService.sensevoice` 枚举 + 选择分支;`routers/listen/receiver.py` 加 socket 构建分支(自包含)

## 验证(2026-08-20, Mac arm64 CPU)

- 锁定的 `sherpa-onnx==1.13.4` + 官方 int8 模型，中文 `say` 音频经
  16 kHz PCM → 当前 `SenseVoiceSocket`；1 秒首窗口在 session end 前输出
  “今天天气。”，尾段 drain 输出“很好，我们去公园散步。”，socket 未死亡。
- 62 个 STT/TTS provider、callback、incremental/finalize/drain focused tests
  全过；SenseVoice 未配置时不会隐式选择 managed default。

## 说明

SenseVoice 模型本身是离线解码器；服务适配器通过有界音频窗口与 VAD 边界提供增量结果。它不会把整场会话留到断开后处理，也不会在 event loop 内同步推理。窗口之间没有语言模型上下文，因此精度/标点仍应通过真实音频门禁评估。

当前 speaker 能力是**窗口级聚类**，不是帧级 speaker diarization：同一 ASR/VAD 窗口内发生的换人不会被切开。sherpa 的 `OfflineSpeakerDiarization` 还要求额外的 Pyannote segmentation ONNX；本实现不偷偷下载该模型，也不在仅挂载 embedding 模型时宣称支持该能力。需要帧级等价时，应新增显式只读 segmentation model 挂载与启动期校验，并把 diarization turns 与 ASR 时间轴对齐后再开放该模式。
