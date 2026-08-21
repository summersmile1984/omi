# MOSS Pipeline: 转写 + 说话人分离 + 说话人识别(无 GPU)

`backend/utils/moss_pipeline/` — 用 OpenMOSS 官方 API 打通"转写 → 说话人分离 → 说话人识别"一条链路,服务器无需 GPU。

> 这里仅属于 hosted `mosi.cn` authority（selector token `moss`）。运营方自有的
> mlx-audio 服务使用独立 token `mlx_moss_diarize` 和
> `backend/utils/mlx_moss_diarize/`；两者不共享 endpoint、client、凭据或 fallback。

## 链路

```
音频 (WAV)
  │
  │ ① MOSS 官方 API (api.mosi.cn)
  │   moss-transcribe-diarize (diarize=true)
  │   → segments[] {start, end, text, speaker: S01/S02}
  ▼
S01/S02 匿名标签 + 时间戳
  │
  │ ② 片段切分 (_wav_slice)
  │   每说话人取最长片段,从原音频切出 WAV
  ▼
speaker 音频片段
  │
  │ ③ 说话人 embedding (可插拔,默认 CPU)
  │   wespeaker-resnet34 / pyannote → 256 维向量
  ▼
query embedding
  │
  │ ④ 与 people 列表余弦匹配 (compare_embeddings + threshold)
  │   每人只匹配一个说话人 (diarization 保证互斥)
  ▼
speaker_map: S01 -> (person_id, person_name)
  │
  │ ⑤ 回填
  ▼
带 person_id 的 transcript segments → memory 提取 (speaker_profiles)
```

## 组件

| 文件 | 职责 |
|---|---|
| `moss_client.py` | MOSS API 封装:上传 `/v1/files`、转写 `/v1/audio/transcriptions`、`moss-transcribe` / `moss-transcribe-diarize`、鉴权/错误/异步轮询 |
| `pipeline.py` | `MossSpeakerPipeline`:上传 → 转写+分离 → 切片 → embedding → 匹配 → person_id 回填 |

## 用法

```python
from utils.moss_pipeline.pipeline import MossSpeakerPipeline

pipe = MossSpeakerPipeline()  # 读取 MOSS_API_KEY 环境变量
wav = open("meeting.wav", "rb").read()
people = {
    "alice": {"name": "Alice", "embedding": <(1,256) np.ndarray>},
    "bob":   {"name": "Bob",   "embedding": <(1,256) np.ndarray>},
}
result = pipe.run(wav, people, transcribe_model="moss-transcribe-diarize")
for seg in result.segments:
    print(seg.start, seg.end, seg.speaker, seg.person_id, seg.text)
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MOSS_API_KEY` | — | **必填**。api.mosi.cn 控制台生成 |
| `MOSS_API_BASE` | `https://api.mosi.cn` | API base |
| `MOSS_TIMEOUT_SECONDS` | `120` | 请求超时 |
| `SPEAKER_EMBEDDING_API_URL` | — | 说话人 embedding HTTP 端点(`/v2/embedding`,CPU 兼容) |

## 接入现有路径

### sync 路径(现成衔接点)
`utils/sync/pipeline.py:879 identify_speakers_for_segments` 已实现识别逻辑(embedding + "I am X" 文本)。MOSS pipeline 的 `result.segments` 形状(speaker/text/start/end)与其 `TranscriptSegment` 兼容:
- 用 MOSS 替换上游 STT+diarizer → 得到 S01/S02 + 时间戳
- 复用现有 `_extract_speaker_clip_wav`(sync/pipeline.py:833)做切片
- 复用现有 `utils/stt/speaker_embedding` 做 embedding + 匹配

### 识别门控(现有缺口)
`should_enable_speaker_identification`(utils/transcribe_decisions.py:161)要求 `private_cloud_sync_enabled or has_speech_profile`。用 MOSS pipeline 时识别在本地 CPU 做、成本低,建议对非 custom-STT 用户放开(见 omi-speaker-identification-roadmap.md)。

### 本地 CPU 识别配置
- embedding 模型: wespeaker-voxceleb-resnet34-LM(~25MB)或 pyannote/embedding,CPU 即可(>100x 实时)
- 匹配阈值: `SPEAKER_MATCH_THRESHOLD = 0.45`(utils/stt/speaker_embedding.py:19)
- 需要时本地装: `pip install pyannote.audio torch`(或复用 diarizer 镜像的 embedding 端点)

## 验证(2026-08-09 实测)

| 步骤 | 结果 |
|---|---|
| 双说话人中文音频上传 | ✅ |
| `moss-transcribe-diarize` 分离 | ✅ 2 segments:S01[0.1-3.6] / S02[3.9-6.5],中文准确 |
| 切片 + embedding + 匹配 | ✅ S01→Alice 回填,S02 超阈值不误配 |
| 普通转写 `moss-transcribe` | ✅ 中文准确 |

## 注意事项

- **MOSS 只有分离,无识别**:返回 S01/S02 匿名标签,识别靠本地 embedding 匹配
- 定价:官方称曾限时免费,**正式价格未公布**(跟踪项)
- MOSS 不支持内联 base64 音频,需 file/url
