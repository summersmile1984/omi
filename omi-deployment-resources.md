# 系统部署资源全景调查 (memweft/Omi fork)

日期: 2026-08-09 · 调查范围: 整个系统部署所需数据库、中间件、服务器资源、AI API 接口类型
数据来源: docker-compose、backend/runtime_images.json、backend/charts/*、backend/deploy/runtime_env.yaml、utils/llm、config/stt_provider_policy.py、启动 QoS 表

## 一、数据库

| 数据库 | 用途 | 部署方式 | 本地替代(shim) |
|---|---|---|---|
| **Firestore** (主存储) | 全部业务数据: users/conversations/memories/action_items/goals/apps/llm_usage 等 88 模块 | 生产 GCP;本地三 emulator | **firestore_pg shim → PostgreSQL**(零改动) |
| **PostgreSQL** (shim 目标) | firestore_pg shim 的 JSONB 存储 | 本地 docker(postgres:16,端口 5434) | 就是它 |
| **Redis** | 缓存/限流/Lua 脚本/锁/pub-sub/地理 | 生产 Memorystore;本地 redis:7 | 本地容器 |
| **Pinecone** (向量库) | 语义检索(记忆/会话 embedding 向量) | 生产 Pinecone Cloud | pgvector(未接) |
| **GCS** (对象存储) | 音频/文件/私人云同步(utils/other/storage.py) | 生产 GCS | Storage emulator(9199) |
| **Neo4j** | 知识图谱(历史遗留) | 已废弃,现 Firestore 承载 | — |
| BigQuery/PubSub | 无使用 | — | — |

## 二、中间件/基础设施

| 中间件 | 用途 | 部署 |
|---|---|---|
| **GKE / Cloud Run** | 容器编排:后端、各子服务 | GCP |
| **Cloud Tasks** | 异步任务队列:sync-jobs/audio-merge/account-deletion/conversation-finalization | GCP |
| **Cloud Scheduler** | cron: notifications-job、memory-maintenance-job | GCP |
| **Modal** | 无服务器 GPU(speech profile 等) | Modal Cloud |
| **GCS** | 见上 | GCP |
| **Firebase Auth / FCM** | 认证/推送 | GCP |
| **LLM Gateway** | Omi 托管 LLM 自动车道(内部服务) | GKE |

## 三、服务器资源 (子服务)

| 服务 | 镜像(runtime_images) | CPU | 内存 | GPU | 端口 | 副本 |
|---|---|---|---|---|---|---|
| **backend** | backend/Dockerfile | — | — | 无 | 8080 | 多(Cloud Run) |
| **desktop-backend** | Dockerfile.desktop_backend | — | — | 无 | 8100 | — |
| **pusher** | pusher/Dockerfile | CPU | — | 无 | WS | — |
| **diarizer** | diarizer/Dockerfile | CPU | — | **nvidia.com/gpu: 1** | 8080 | prod+dev |
| **parakeet** (STT) | parakeet/Dockerfile(.nim) | CPU | — | **nvidia.com/gpu: 1** | 8080 | prod+dev |
| **vad** (语音活动检测) | modal/Dockerfile | CPU | — | **nvidia.com/gpu: 1** | 9091 | prod+dev |
| **nllb-translation** (翻译) | nllb_translation/Dockerfile | CPU | — | **nvidia.com/gpu: 1** (nvidia-l4) | 8080 | prod+dev |
| **agent-proxy** | agent-proxy/Dockerfile | CPU | — | 无 | WS | — |
| **llm-gateway** | — | CPU | — | 无 | 8080 | — |
| **agent-vm** | agent_vm/Dockerfile | CPU | — | 无 | 8080 | 每用户 VM |
| **notifications-job** | modal/Dockerfile.notifications_job | CPU | — | — | cron | — |
| **memory-maintenance-job** | modal/Dockerfile.memory_maintenance_job | CPU | — | — | cron | — |

注意: GPU 服务(diarizer/parakeet/vad/nllb)通过 GKE nodeSelector 挂 `nvidia.com/gpu`;本地无 GPU 时全部走 fallback 或 emulator。

## 四、AI API 接口类型

### LLM (文本生成) — utils/llm/providers.py + QoS 表
| Provider | 接入方式 | 典型模型 (QoS) |
|---|---|---|
| **OpenAI** | ChatOpenAI (OPENAI_API_KEY) | gpt-5.6-luna(主力)、gpt-5-nano(轻量) |
| **Anthropic** | (legacy BYOK/桌面 fallback) | claude-sonnet-4-6(chat_agent) |
| **Google Gemini** | get_or_create_gemini_llm (GEMINI_API_KEY) | gemini-2.5-flash-lite(翻译/会话标题/onboarding/trends) |
| **OpenRouter** | OpenAI 兼容 (OPENROUTER_API_KEY, base_url) | gemini-3-flash-preview(wrapped_analysis) |
| **Perplexity** | OpenAI 兼容 | sonar-pro(web_search) |
| **LLM Gateway** | Omi 托管 `omi:auto:*` 车道(网关优先) | luna/nano 等 |

### Embedding (向量)
- **OpenAI text-embedding-3-large** (utils/llm/clients.py:661)→ Pinecone

### STT (语音识别) — config/stt_provider_policy.py 权威
| Provider | 类型 | 表面 |
|---|---|---|
| **Deepgram Cloud** | 托管 API (DEEPGRAM_API_KEY) | STREAMING (dg-nova-3) |
| **Deepgram Self-hosted** | 自托管 GPU | STREAMING |
| **Parakeet** (GPU 子服务) | 自托管 | STREAMING/BATCH/PTT (parakeet-tdt-0.6b-v3 / parakeet-rnnt-1.1b) |
| **Modulate** | 托管 API | STREAMING (多语言) |

### TTS (语音合成)
- **ElevenLabs** (routers/tts.py 服务端代理, 限流: 50/min, 1万字符/日)

### 翻译
- **Gemini** (utils/translation.py): gemini-2.5-flash-lite
- **NLLB-200** (自托管 GPU 子服务, 备选, HOSTED_TRANSLATION_API_URL)

### 其他 AI 面
- **Hume AI**(情感检测, optional)
- **VAD**(pyannote/silero, GPU 子服务)
- **Diarization / speaker identification**(pyannote/speechbrain, GPU 子服务)

## 五、本地部署 (shim 模式) 全栈

```
本地:
  Firebase emulators (firestore 8080 / auth 9099 / storage 9199) — dev/docker-compose.dev.yml
  PostgreSQL 16 (127.0.0.1:5434) — firestore_pg shim 目标
  Redis 7 (127.0.0.1:6379) — 缓存/限流
  backend (uvicorn :8100) + FIRESTORE_PG_DSN
  STT/TTS/LLM → 需真实 API key (offline 无 key 时 fail-open / 500)
```

## 六、关键结论

1. **主存储 Firestore 是唯一"数据库"依赖**;Pinecone(向量)、GCS(对象)、Redis(缓存)为辅助。Neo4j/BigQuery/PubSub 已不用。
2. **GPU 需求集中在 4 个自托管 AI 服务**: diarizer / parakeet / vad / nllb-translation,各 1 块 GPU(GKE nvidia.com/gpu)。
3. **AI API 分 6 类**: LLM(OpenAI/Anthropic/Gemini/OpenRouter/Perplexity/网关)、Embedding(OpenAI)、STT(Deepgram/Parakeet/Modulate)、TTS(ElevenLabs)、翻译(Gemini/NLLB)、感知(VAD/说话人识别/Hume)。
4. **本地 shim 栈已覆盖除 AI API 外的全部**: 数据库(Firestore→PG)、Redis、Storage 均可本地跑;AI API 需真实 key。
