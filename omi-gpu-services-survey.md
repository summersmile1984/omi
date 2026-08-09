# GPU 服务调研结论:API 替代 + 中文更优模型 + 最低 GPU 型号

关联文档: `omi-architecture-analysis.md`(GPU 管线)、`omi-cloud-neutral-postgres-migration.md`(GPU 部署面 §1.3/§2.6)、`omi-deployment-resources.md`(服务器资源 §三)。
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

| 服务 | 可替代的托管 API | 中文表现 | 结论 |
|---|---|---|---|
| **parakeet/STT** | Deepgram(生产已配,优先)、Modulate(已配)、Soniox(移动端默认) | Deepgram nova-3 支持中文 | **可用 API 完全替代**,已配 Deepgram → 本地无需自托管 parakeet |
| **diarizer** | 无直接云 API(Deepgram diarization 附带);自托管可选 pyannote | 中文一般 | 保留自托管(见 §三 中文更优)或改用 Deepgram 附带 |
| **vad** | pyannote 极轻(<1GB),无必要换 API | 语言无关 | 保留自托管,CPU 也能跑 |
| **nllb-translation** | **已弃用**:生产 translation 走 Gemini(2.5-flash-lite),NLLB 是 fallback | Gemini 中文优 | **GPU 可退役**,NLLB 只是备选 |

**结论**:4 个 GPU 服务里,**nllb 和 parakeet 可被 API 替代**(翻译走 Gemini、STT 走 Deepgram/Modulate),实际"必须自托管 GPU"的只剩 **diarizer + vad**。diarizer 还可用 Deepgram 的 speaker diarization 能力合并掉 → 理论上**自托管 GPU 服务可全部退役**。

## 三、中文更优模型推荐(若保留自托管)

### STT(替代 parakeet,中文)
| 模型 | 参数 | 显存 | 中文 | 备注 |
|---|---|---|---|---|
| **SenseVoice-Small** (FunASR) | ~300M | <1GB | ★★★★★ 中英日+粤语 | GPU 169x 实时,CPU 17x |
| **Paraformer-Large** (FunASR) | 220M | 1-2GB | ★★★★★ 纯中文+时间戳+热词 | 最佳性价比 |
| **Fun-ASR-Nano** (LLM-ASR) | Qwen3-0.6B 解码 | 需 GPU | ★★★★★ 中英日+7方言+26口音 | 旗舰,长尾/难例最好 |
| parakeet-tdt-0.6b-v3 | 600M | 2-2.5GB | ❌ 无中文 | 现状,应替换 |

### Diarizer(替代 pyannote,中文)
| 模型 | DER(AISHELL-4) | 中文 | 备注 |
|---|---|---|---|
| **pyannote community-1** | 11.7% | ★★★★ | 现状,通用最佳 |
| **3D-Speaker + CAM++** | 13.3% | ★★★★★ | 中文优化,Apache 2.0 |
| **FunASR CAM++ pipeline** | 优秀 | ★★★★★ | 中文生产集成,可与 STT 复用一套 |

## 四、仅跑起来的最低 GPU 型号

基于各模型显存需求(全部 INT8/FP16 推理,非训练):

| 服务 | 需要显存 | 最低可用 GPU | 更稳妥 |
|---|---|---|---|
| parakeet (STT) | ~2GB | **GTX 1050 Ti / GTX 1660 4GB** | RTX 3060 12GB |
| diarizer (pyannote) | ~4GB | **RTX 2060 6GB** | RTX 3060 12GB |
| vad | <1GB | 任何 NVIDIA(或 CPU) | 集成进 diarizer 卡 |
| nllb | ~2GB | GTX 1050 Ti 4GB | T4 16GB |
| **四合一(推荐)** | ≤6GB 峰值 | **RTX 2060 6GB 或 RTX 3060 12GB** | T4/L4 |

**结论(最低 GPU)**:
- **只跑通**: 一块 **RTX 2060 6GB**(~¥800 二手)足以跑全部 4 个服务(INT8 量化后峰值 <6GB)。
- **中文替换后更省**: SenseVoice/Paraformer(1-2GB)+ CAM++(中文 diarizer),单卡 **GTX 1660 4GB 或 RTX 3050 4GB** 就够。
- **若走 API 替代**(翻译→Gemini,STT→Deepgram): 自托管 GPU 仅 diarizer+vad,一块 **GTX 1660 4GB** 即可;若 diarizer 也走 Deepgram,GPU 可全免。

## 五、建议路径(本地/自托管)

1. **翻译**: nllb-translation GPU 服务**退役** → 生产已走 Gemini(中文优)。省 1 卡。
2. **STT**: parakeet → 本地用 **FunASR SenseVoice-Small / Paraformer**(中文)或直接 Deepgram API。省 1 卡 + 中文能力大幅提升。
3. **diarizer**: pyannote → 保留或用 **3D-Speaker/CAM++**(中文优)。
4. **vad**: 并入 diarizer 卡或 CPU。
5. **最低硬件**: 单块 **RTX 2060 6GB**(保守)或 **GTX 1660 4GB**(中文替换后)即可覆盖剩余自托管 GPU 需求。

## 六、验证状态

- 模型清单/显存: 已从代码确认(parakeet NeMo、pyannote、nllb CTranslate2)+ 最新 HF 数据核对。
- parakeet 中文支持: **确认不支持**(25 欧洲语言)。Deepgram nova-3 / FunASR 支持中文。
- nllb 中文: NLLB-200 支持 200+ 语言含中文,但生产已弃用(走 Gemini)。
- 注意: pyannote.audio 4.0.3 有 VRAM 回归 bug(长音频 >9.5GB),若升级需锁 3.3.2。
