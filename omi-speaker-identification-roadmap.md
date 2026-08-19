# 说话人识别技术实现路线方案

日期: 2026-08-09 · 前置: `omi-speaker-identification-survey.md`(现状缺口)
范围: 纯技术实现路线(不涉付费)。目标: 让"识别(speaker identification)→ memory 人物归属"全链路稳定工作。

## 一、目标架构

```
音频流
  │
  ├─ ① Diarization(分离)   [已有]   → speaker_id (SPEAKER_00/01)
  ├─ ② Identification(识别) [门控]   → person_id (这是 Alice)
  │        │
  │        └─ ③ 自动建档(未知说话人)  → 新 person + speech sample + embedding
  │
  ▼
conversation (segments 带 speaker_id + person_id)
  │
  ▼
④ Memory 提取(speaker-aware) → ent_person_N / ent_speaker_N + speaker_identity_claim
  │
  ▼
memory 事实归属: "Alice 说了 X" / "这是用户的朋友 Alice"
```

## 二、技术实现路线(4 阶段)

### 阶段 1: 放宽识别门控(核心缺口,改动最小)

**现状**: `should_enable_speaker_identification` 要求 `private_cloud_sync_enabled or has_speech_profile`。

**方案**: 将识别从"账号级特性"改为"技术能力"——**所有非 custom-STT 用户都跑识别**。识别本身只是 embedding 余弦匹配(成本低),不应被账号状态门控。

```python
# utils/transcribe_decisions.py
def should_enable_speaker_identification(*, use_custom_stt: bool, **_) -> bool:
    return not use_custom_stt   # 简化:自定义 STT 用户(自带说话人)除外
```

**改动面**:
- `utils/transcribe_decisions.py:161` 门控函数
- `routers/listen/runtime.py:318` 调用点
- 依赖: people 列表 + user 自身 embedding(speakers.py 已实现加载)

**风险**: 无。识别是尽力而为,匹配不到就 fallback 到 `SPEAKER_N` 标签。

### 阶段 2: 自动建档(未知说话人 → person)

**现状**: 识别只匹配已有 people;陌生声音永远匹配不上 → 无法沉淀新人物。

**方案**: 匹配失败时,把该 speaker 的音频片段缓存为"候选新说话人",满足条件后:
1. 收集 ≥N 段清晰语音片段(用户确认或自动)
2. 提取 embedding(`extract_embedding_from_bytes`)
3. 创建 `person`(`/v1/users/people`)+ 存 speech sample + speaker_embedding
4. 后续会话同一声音 → 自动匹配上

**改动面**:
- `routers/listen/speakers.py` match 失败分支 → 新 `CandidateSpeaker` 累积
- `database/users.py` 新 `add_candidate_speaker` / `promote_candidate_to_person`
- 需用户确认入口(前端)或后端自动(风险: 误建)
- 模型: 现有 wespeaker/pyannote embedding(无需新模型)

### 阶段 3: Memory 回流(识别结果进 memory)

**现状**: conversation 的 person_id 没有显式传递到 memory 提取上下文;`speaker_identity_claim` 恒空。

**方案**:
1. conversation 持久化 `speaker_to_person` 映射(`conversation.speaker_map` 或 segments 上的 person_id)——`emit_speaker_suggestion` 已有,需落库
2. memory 提取输入带上 `speaker_profiles`(SpeakerProfileSnapshot 已建模:models.py:428)+ segment→person 映射
3. `production_like_model.py` 的 prompt 已支持 `ent_speaker_N`/`ent_person_N`,补上真实 person 名
4. 提取后: `speaker_identity_claim` 从映射解析,`speaker_confirmed=True`

**改动面**:
- `database/conversations.py` 持久化 speaker_map
- `utils/memory_ingestion/pipeline.py` 构造输入时注入 speaker→person
- `utils/memory_ingestion/adapters/production_like_model.py` prompt 上下文
- `utils/memory_ingestion/models.py` `speaker_identity_claim` 消费

### 阶段 4: OpenMOSS 整合(自托管,替换 STT+分离)

**现状**: parakeet(无中文)+ diarizer(pyannote)两个 GPU 服务;识别靠 wespeaker embedding 单独走。

**重要澄清**: MOSS-Transcribe-Diarize 0.9B 是**端到端转写+说话人分离(diarization)**模型——输出 `[S01]/[S02]` **匿名相对标签**,**没有识别(identification)能力**。其 Whisper-Medium 风格 audio encoder 的内部 embedding 不对外暴露为 speaker embedding,也无 voiceprint/enrollment 机制。

**方案**:
- 用 OpenMOSS 替换 parakeet STT + diarizer 分离(4 GPU 服务 → 1)
- **识别仍需独立通道**: OpenMOSS 输出的 `[S01]` 标签 → 从音频片段提取 speaker embedding(现有 wespeaker/pyannote)→ 与 people 列表匹配 → person_id(阶段 1-3 的识别链路保留,只换掉上游的分离/转写)
- 长期架构: **OpenMOSS(转写+分离)+ 独立 embedding 识别**(或未来模型若支持 enrollment 再合并)

```
OpenMOSS 0.9B (转写+分离 [S01]/[S02])
   │
   ├─ 文本+时间戳 → 转录
   └─ speaker 音频片段 → wespeaker embedding → 与 people 匹配 → person_id (识别,独立通道)
```

## 三、关键技术决策

| 决策点 | 选项 | 推荐 | 理由 |
|---|---|---|---|
| 识别模型 | wespeaker(现状) / ECAPA-TDNN / OpenMOSS | 保留 wespeaker/ECAPA(OpenMOSS 无识别能力) | OpenMOSS 只做分离,识别需独立 embedding 匹配 |
| **识别算力** | CPU / GPU | **CPU 即可** | wespeaker-voxceleb-resnet34-LM ~25MB(ResNet34,256 维),CPU 实时轻松;diarizer 服务已 `cuda if available else cpu` 双支持 |
| 自动建档触发 | 用户确认 / 自动 | 用户确认(风险低) | 误建 person 污染 people 列表 |
| speaker_map 存哪 | conversation doc / segments / 独立集合 | conversation doc 内嵌 | 与 transcript 同生命周期,读取简单 |
| memory 注入 | prompt / 结构化字段 | 结构化字段(speaker_profiles) | prompt 已有,结构化更稳 |
| 门控 | 移除 / 保留账号级 | 移除(技术能力) | 识别成本低,不应用账号状态挡 |

## 四、算力评估:声纹识别 CPU 够用,GPU 只留给分离

| 环节 | 计算量 | CPU 可行? | 说明 |
|---|---|---|---|
| **声纹 embedding**(wespeaker-resnet34-LM) | 单片段 <几十 ms(CPU) | ✅ | ~25MB 模型(ResNet34,256 维),实时率 >100x |
| **余弦匹配**(256 维 vs people 列表) | 微秒级 | ✅ | 纯向量点积 |
| **说话人分离 diarization**(pyannote/OpenMOSS) | 重(长音频聚类) | ⚠️ 慢 | **需要 GPU**(OpenMOSS L4 / pyannote GPU) |
| **OpenMOSS 转写** | 重 | ❌ | 需 GPU(见 omi-gpu-services-survey.md) |

**结论**:
- **识别(embedding+匹配) = CPU 任务**,可与 GPU 服务解耦,独立部署(普通机器即可)。
- **GPU 只留给 OpenMOSS(转写+分离)**——两者解耦,识别不绑定 GPU 服务。
- 现有 diarizer 服务已 `cuda if available else cpu` 双支持,embedding 端点天然可 CPU 跑。

## 五、OpenMOSS 输出衔接(识别如何接)

```
OpenMOSS 0.9B 输出 (vLLM/SGLang, OpenAI 兼容):
  [0.11][S01] text [1.03]
  [1.11][S02] text [2.42]

衔接步骤:
  1. 解析 [Sxx] 标签 + [start]/[end] 时间戳 → speaker 分段
  2. 每段从音频切出片段(_extract_speaker_clip_wav 已有)
  3. 片段 → wespeaker embedding(CPU)
  4. embedding 与 people 列表余弦匹配 → person_id
  5. person_id 回填 transcript segment + speaker_map
  6. memory 提取消费 speaker_profiles
```

- 衔接点: OpenMOSS 输出的时间戳 + `[Sxx]` 标签 与现有 `_extract_speaker_clip_wav`(sync/pipeline.py:833)天然匹配——**不需要改切片逻辑**,只把上游 STT 换成 OpenMOSS、把识别通道接到 embedding(CPU)即可。

## 四、依赖与模型

- 阶段 1-3: 无新模型。复用现有 pyannote wespeaker embedding + people 列表 + user speech profile。
- 阶段 4: OpenMOSS 0.9B(L4, 见 omi-gpu-services-survey.md)。
- 可选增强: 说话人 embedding 用 **3D-Speaker/CAM++**(中文更优,AISHELL-4 DER 13.3% vs pyannote 11.7%,中文优化)。

## 五、验证标准

1. 门控移除后,无 speech profile 用户也能收到 `emit_speaker_suggestion`(person_id)。
2. 识别出的人名出现在 memory 提取的 speaker_profiles。
3. 记忆事实带 `speaker_identity_claim` + `speaker_confirmed=True`(而非 uncertain)。
4. 新说话人经自动建档后,第二次会话能匹配上同一 person。
5. 回归: 识别失败时 fallback 到 `SPEAKER_N` 标签,不影响转录。

## 六、MOSS 官方 API 实测(2026-08-09, api.mosi.cn)

平台: https://platform.mosi.cn(API base: https://api.mosi.cn)。已用真实 API key 端到端验证:

### 可用模型(语音识别类)
- `moss-transcribe`: 普通转写,`POST /v1/audio/transcriptions` → `{text}`
- `moss-transcribe-diarize`: 多说话人转写,`diarize=true` → `segments[]` 含 `start/end/text/speaker(S01)`

### 实测结果
| 测试 | 结果 |
|---|---|
| 文件上传 `/v1/files` (purpose=transcription) | ✅ 512MB 上限,音视频容器(MOV/MP4/MPG/WebM)均可 |
| `moss-transcribe` 中文音频 | ✅ 准确:"大家好，我们今天讨论项目进度。" |
| `moss-transcribe-diarize` (diarize=true) | ✅ 返回 `segments[].speaker` = `S01/S02` 匿名标签 |
| 鉴权 | ✅ `Authorization: Bearer <key>` |
| 流式 | ✅ `stream=true` SSE(`transcript.segment.done` 带 speaker) |
| 异步 | ✅ `async=true` + 任务查询 |

### 关键结论(与自托管一致)
- **MOSS 官方 API = 转写 + 分离(diarization)**,输出 `S01/S02` 匿名标签
- **无识别(identification)**:不能匹配到已知人名,与自托管 0.9B 能力一致
- **对 Omi 的价值**:可直接作为无 GPU 时的中文转写+分离 API;识别仍需本地 wespeaker(CPU)匹配
- 定价: 官方称曾限时免费,**正式价格未公布**(跟踪项)
