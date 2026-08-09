# 说话人链路现状:分离(Diarization)vs 识别(Identification)对 Memory 的价值

日期: 2026-08-09 · 调查范围: 说话人分离/识别的代码实现、调用点、对 memory 的价值与缺口

## 一、结论先行

**识别(identification)环节代码存在,但对多数用户实际是缺位的。**

- **分离(diarization)**: 全链路都有 —— 把音频切成 `SPEAKER_00/01/...` 相对标签。
- **识别(identification)**: 代码有(`identify_speakers_for_segments` / `routers/listen/speakers.py` 实时匹配),但**被 `should_enable_speaker_identification` 门控**:
  ```python
  return not use_custom_stt and (private_cloud_sync_enabled or has_speech_profile)
  ```
  → 用户**没开私有云同步、且没有 speech profile 时,identification 完全不跑**,只有 diarization 标签。
- **对 memory 的价值**: v3 memory 管线是 speaker-aware 的(消费 `person_id`、产出 `ent_speaker_N`、有 `speaker_confirmed/speaker_uncertain` 置信度),但**上游没给 person_id 时 → memory 只能标 `speaker_uncertain`**,无法把"这是 Alice 说的"沉淀进记忆。**这是当前缺口。**

## 二、现状矩阵(代码确认)

| 环节 | 实现 | 调用点 | 触发条件 |
|---|---|---|---|
| **Diarization(分离)** | pyannote/diarizer + parakeet 聚类(`_parakeet_assign_speaker_sync`) | live transcribe / sync 全程 | 始终 |
| **Identification(识别)** | `utils/sync/pipeline.py:879 identify_speakers_for_segments`(embedding 余弦 + "I am X" 文本) | **仅 sync 路径**(pipeline:1106) | sync 流程 |
| **Identification(实时)** | `routers/listen/speakers.py`(用户 embedding + people 列表匹配) | live listen | **`speaker_id_enabled`** = 私有云同步或 speech profile |
| **Memory 消费** | `production_like_model.py`(speaker→`ent_speaker_N`→`speaker_identity_claim`/置信度) | memory v3 管线 | 依赖上游 person_id |

## 三、Memory 的价值(为什么重要)

speaker/person 归属对 memory 的价值链路:
1. **事实归属**: "Alice 说她下周交方案" → 如果识别出 Alice,memory 可记为 `ent_person_1 说了 X`(第三方主体归因 `subject_attribution=third_party`)。
2. **人物关系**: 长期识别同一人 → memory 可沉淀"Alice 是用户的同事/家人"(人物图谱)。
3. **speaker_identity_claim**: `utils/memory_ingestion/models.py:693` 已建模 `speaker_identity_claim`,但只有 identification 给到 person_id 才被填上。
4. **置信度**: `speaker_confirmed` vs `speaker_uncertain`(models.py:886)——识别缺失 → 恒为 uncertain → memory 质量打折。

## 四、当前缺口

1. **门控过严**: identification 只在 `private_cloud_sync_enabled or has_speech_profile` 时跑。多数免费用户两者皆无 → 只有 `SPEAKER_00` 标签 → memory 无人物归属。
2. **live vs sync 不一致**: sync 路径有完整 identification(`identify_speakers_for_segments`),live transcribe 路径依赖门控 + `speaker_auto_assign` 参数(默认 `disabled`)。
3. **数据面**: 识别依赖 people 列表的 `speaker_embedding`/`speech_samples`,而 people 是用户手动建的(`/v1/users/people`)——没有"自动认识新说话人并建档"的能力。
4. **memory 落地**: 即使识别出 person_id,memory 的 `speaker_identity_claim` 消费仍受限于 conversation 传递(person_id 是否进入 memory 提取上下文)。

## 五、接入方案(填缺口)

| 方案 | 说明 | 模型 |
|---|---|---|
| **A. 放宽门控** | identification 对所有非 custom-STT 用户开启(embedding 匹配成本低) | 现有 pyannote wespeaker + people embedding |
| **B. OpenMOSS 整合** | OpenMOSS 0.9B 自带说话人**分离**+时间戳(替换 parakeet+diarizer);**注意:OpenMOSS 无识别能力**,识别仍需独立 embedding 匹配通道(见 omi-gpu-services-survey.md) | OpenMOSS 0.9B(分离)+ wespeaker(识别) |
| **C. 自动建档** | 识别到未知说话人 → 自动创建 person + 收集 speech sample(需用户确认) | ECAPA-TDNN / pyannote embedding |
| **D. 记忆回流** | conversation 的 person_id 显式传入 memory 提取;`speaker_identity_claim` 强制要求 | 现有 v3 管线打通 |

## 六、结论

- 说话人**分离**已有,说话人**识别**代码已有但**被门控、多数用户实际缺位**。
- 对 memory 的价值明确(v3 已建模 speaker→entity→claim 链路),缺的是**上游识别稳定供给 person_id**。
- **推荐**: 方案 A(放宽门控)+ D(memory 回流),长期走 B(OpenMOSS 整合,自托管推荐)。
