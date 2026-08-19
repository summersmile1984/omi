# GPU 服务调研结论:API 替代 + 中文更优模型 + 最低 GPU 型号

关联文档: `omi-architecture-analysis.md`(GPU 管线)、`omi-cloud-neutral-postgres-migration.md`(GPU 部署面 §1.3/§2.6)、`omi-deployment-resources.md`(服务器资源 §三)、`docs/doc/developer/backend/transcription.mdx`(STT serving 策略)、`docs/doc/developer/backend/translation_benchmark.mdx`(NLLB 中文基准)。
本文件把"已有调研"补到模型级:确认 4 个 GPU 服务能否用 API 替代、中文更优模型、仅跑起来的最低 GPU。

## 一、现状:4 个自托管 GPU 服务(确认)

| 服务 | 当前模型 | 显存需求 | 生产 GPU |
|---|---|---|---|
| **parakeet** (STT) | `parakeet-tdt-0.6b-v3`(0.6B,批量/25语) + `parakeet-rnnt-1.1b`(1.1B,流式/仅英文) | FP16 ~2-2.5GB, INT8 ~1.2-1.6GB | 1x nvidia.com/gpu |
| **diarizer** (说话人) | `pyannote/speaker-diarization-community-1` + `pyannote/embedding` | ~2-4GB | 1x nvidia.com/gpu |
| **vad** (语音活动检测) | `pyannote/voice-activity-detection` | <1GB | 1x nvidia.com/gpu |
| **nllb-translation** (翻译) | `nllb-200-distilled-600M` (CT2 int8_float16) | int8 ~1.1GB, 实际 ~2GB | 1x nvidia.com/gpu (nvidia-l4) |

**关键确认:parakeet-tdt-0.6b-v3 只支持 25 种欧洲语言,不含中文**(Granary 语料)。当前 STT 栈对中文无效;diarizer pyannote 对中文一般(AISHELL-4 DER 11.7%)。

## 二、API 替代方案矩阵

> 修正: 生产 STT serving 默认是 **`modulate-velma-2, parakeet`**(transcription.mdx 权威);Deepgram 需自托管显式启用(`DEEPGRAM_SELF_HOSTED_ENABLED=true`),**不是** api.deepgram.com 兜底。Modulate velma-2 支持 `zh`。

| 服务 | 可替代的托管 API | 中文表现 | 结论 |
|---|---|---|---|
| **parakeet/STT** | Modulate velma-2(生产默认,支持 zh)、Deepgram 自托管 | velma-2 支持中文 | **可用 API 替代**;或自托管 **OpenMOSS 0.9B**(中文+说话人**分离**,见 §三) |
| **diarizer(分离)** | OpenMOSS 0.9B 自带说话人分离(多说话人 SOTA);Deepgram diarization 附带 | 中文好 | **OpenMOSS 可替代独立 diarizer** |
| **identification(识别)** | 无直接云 API;需 embedding 匹配(现有 wespeaker/pyannote)| 见 speaker-identification-survey | **OpenMOSS 无识别能力**,识别仍需独立 embedding 匹配(见 omi-speaker-identification-survey.md) |
| **vad** | pyannote 极轻(<1GB),无必要换 API | 语言无关 | 保留自托管,CPU 也能跑 |
| **nllb-translation** | **已弃用**:生产 translation 走 Gemini(2.5-flash-lite),NLLB 是 fallback(见 translation_benchmark.mdx) | Gemini 中文优 | **GPU 可退役**,NLLB 只是备选 |

**结论**:4 个 GPU 服务里,**nllb 和 parakeet 可被 API 替代**(翻译走 Gemini、STT 走 Deepgram/Modulate),实际"必须自托管 GPU"的只剩 **diarizer + vad**。diarizer 还可用 Deepgram 的 speaker diarization 能力合并掉 → 理论上**自托管 GPU 服务可全部退役**。

## 三、中文更优模型推荐(若保留自托管)

### STT(替代 parakeet,中文)
| 模型 | 参数 | 显存 | 中文 | 备注 |
|---|---|---|---|---|
| **OpenMOSS 0.9B 自托管(L4)** | 0.9B | 1 张 L4(月处理 5,000-7,000h) | ★★★★★ 中文转写+说话人**分离**+时间戳+声学事件 | **仅分离(diarization),无识别(identification)**;输出 `[S01]/[S02]` 匿名标签;见 omi-subscription-margin.md + omi-speaker-identification-survey.md |
| **SenseVoice-Small** (FunASR) | ~300M | <1GB | ★★★★★ 中英日+粤语 | GPU 169x 实时,CPU 17x;端侧免费层候选 |
| **Paraformer-Large** (FunASR) | 220M | 1-2GB | ★★★★★ 纯中文+时间戳+热词 | 最佳性价比,中文 CER 10.18%;配 CAM++ 可分离 |
| **Fun-ASR-Nano** (LLM-ASR) | Qwen3-0.6B 解码 | 需 GPU | ★★★★★ 中英日+7方言+26口音 | 旗舰,长尾/难例最好 |
| parakeet-tdt-0.6b-v3 | 600M | 2-2.5GB | ❌ 无中文 | 现状,应替换 |

### Diarizer(替代 pyannote,中文)
| 模型 | DER(AISHELL-4) | 中文 | 备注 |
|---|---|---|---|
| **pyannote community-1** | 11.7% | ★★★★ | 现状,通用最佳 |
| **3D-Speaker + CAM++** | 13.3% | ★★★★★ | 中文优化,Apache 2.0 |
| **FunASR CAM++ pipeline** | 优秀 | ★★★★★ | 中文生产集成,可与 STT 复用一套 |

## 四、仅跑起来的最低 GPU 型号

基于各模型显存需求(全部 INT8/FP16 推理,非训练)。NLLB 的 GPU 内存数据来自仓库内 `translation_benchmark.mdx`(L4 24GB 实测):

| 服务 | 需要显存 | 最低可用 GPU | 更稳妥 |
|---|---|---|---|
| **OpenMOSS 0.9B** (自托管推荐) | 1 张 L4(月 5,000-7,000h) | **T4 16GB** | L4 24GB |
| parakeet (STT) | ~2GB | **GTX 1050 Ti / GTX 1660 4GB** | RTX 3060 12GB |
| diarizer (pyannote) | ~4GB | **RTX 2060 6GB** | RTX 3060 12GB |
| vad | <1GB | 任何 NVIDIA(或 CPU) | 集成进 diarizer 卡 |
| nllb 600M (benchmark) | ~2GB | GTX 1050 Ti 4GB | T4/L4 |
| nllb 1.3B (benchmark) | ~3GB | GTX 1660 6GB | T4/L4 |
| nllb 3.3B (benchmark) | ~5GB | RTX 2060 6GB | L4 24GB |
| **四合一(推荐)** | ≤6GB 峰值 | **RTX 2060 6GB 或 RTX 3060 12GB** | T4/L4 |

**结论(最低 GPU)**:
- **只跑通**: 一块 **RTX 2060 6GB**(~¥800 二手)足以跑全部 4 个服务(INT8 量化后峰值 <6GB)。
- **中文替换后更省**: SenseVoice/Paraformer(1-2GB)+ CAM++(中文 diarizer),单卡 **GTX 1660 4GB 或 RTX 3050 4GB** 就够。
- **若走 API 替代**(翻译→Gemini,STT→Deepgram): 自托管 GPU 仅 diarizer+vad,一块 **GTX 1660 4GB** 即可;若 diarizer 也走 Deepgram,GPU 可全免。

## 五、建议路径(本地/自托管)

1. **翻译**: nllb-translation GPU 服务**退役** → 生产已走 Gemini(中文优)。省 1 卡。
2. **STT(自托管)**: parakeet → **OpenMOSS 0.9B**(中文+说话人分离,多说话人 SOTA,1 张 L4 月处理 5,000-7,000h;详见 omi-subscription-margin.md 路径 A)。省 1 卡 + 中文能力大幅提升 + **说话人分离并入**(可并掉独立 diarizer)。
3. **STT(外包备选)**: 小米 MiMo-V2.5-ASR(OpenAI 兼容可入 llm_gateway,中文第一梯队)或 Deepgram/Modulate。
4. **diarizer**: OpenMOSS 自带说话人分离;纯 diarizer 场景保留 pyannote 或用 3D-Speaker/CAM++(中文优)。
5. **vad**: 并入 diarizer 卡或 CPU。
6. **最低硬件**: 单块 **L4(自托管 OpenMOSS 推荐)**;轻量自托管(不含 OpenMOSS)**RTX 2060 6GB**(保守)或 **GTX 1660 4GB**(中文替换后)即可。

## 六、验证状态

- 模型清单/显存: 已从代码确认(parakeet NeMo、pyannote、nllb CTranslate2)+ 最新 HF 数据核对。
- **NLLB 基准已有仓库文档**: `translation_benchmark.mdx` 实测(L4 24GB INT8):600M ~2GB/1.3B ~3GB/3.3B ~5GB;中文 chrF++ 56→59→63% vs Google 100% → **NLLB 中文明显弱**;文档明确建议中文要专用模型(MADLAD-400-3B 等)。
- **STT 调研已有仓库文档**: `omi-subscription-margin.md`(2026-08)完整 ASR 供应商调研,推荐自托管 **OpenMOSS 0.9B**(中文+说话人分离+时间戳+声学事件,多说话人 SOTA,1 张 L4 月处理 5,000-7,000h)与外包小米 MiMo-V2.5-ASR;端侧 SenseVoice-Small(234M)替代 whisper.cpp tiny。
- **STT serving 默认**: `modulate-velma-2, parakeet`(transcription.mdx 权威);Modulate 支持 `zh`;Deepgram 自托管需显式启用,非默认。
- parakeet 中文支持: **确认不支持**(25 欧洲语言)。
- 注意: pyannote.audio 4.0.3 有 VRAM 回归 bug(长音频 >9.5GB),若升级需锁 3.3.2。
