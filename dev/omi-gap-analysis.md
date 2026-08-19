# Cloud-Neutral 改造遗漏分析

日期: 2026-08-10 · 分支: feature/cloud-neutral-shim · 目标: 找出改造遗漏 + 架构收敛建议

## 一、上游同步遗漏(已处理)

上次合并(46b036126b)后上游累积 **37 个新提交**(LLM backend SSOT/OpenRouter 模型目录、
live STT 失败归属修复、Gmail 选择、BLE 重连修复等)。已 merge(`3822618206`):

- ✅ merge-tree 零冲突,三方合并完成(72 文件,2927+/801-)
- ✅ **语义冲突风险已排除**:上游 receiver.py 删除 sensevoice/mimo 分支、prerecorded_stt.py
  删除 MOSS 是"上游从未有这些"的 diff 视角;三方合并保留我们的分支
- ✅ merge 后验证:MiMo/SenseVoice 分支存活(receiver.py:195-203)、MOSS 存活
  (prerecorded_stt.py:46)、firestore_pg 完好、streaming.py MiMo 分支存活
- ✅ 31 核心测试 + 后端 import + 真实 MiMo ASR 冒烟(`你好，世界，这是测试。`)全过

**结论**: 上游同步完整,无未合并提交,零语义冲突。

## 二、生产迁移遗漏面(仍是上游云依赖)

| 依赖 | 代码位置 | 状态 |
|---|---|---|
| **FCM 推送** | `utils/notifications.py` | ❌ 仍是 Firebase Messaging(上游依赖) |
| **Google/Apple 登录** | `routers/auth.py` | ❌ 仍是上游 OAuth(未替换手机号+验证码) |
| **Stripe 支付** | `utils/stripe.py` | ❌ 仍是上游(大陆不可用) |
| **Pinecone 向量库** | `database/vector_db.py` | ⚠️ env-optional,无 key 跳过 |
| **Neo4j 知识图谱** | `database/knowledge_graph.py` | ⚠️ env-optional |
| **ElevenLabs TTS** | `routers/tts.py` | ✅ 已可切 MiMo(`TTS_PROVIDER=mimo`) |
| **Firebase Auth** | `utils/other/endpoints.py` | ✅ 已可切 BetterAuth(`AUTH_PROVIDER=better_auth`) |

**均为 env-optional**: 后端可无任何云 key 启动(:8102 全 env 验证),但生产功能(推送/登录/支付)缺失。

## 三、端到端验证遗漏

- ⚠️ desktop **语音捕获链路**(真实麦克风 → MiMo STT 转录 → 记忆)未端到端验证(仅单元级 select/socket)
- ⚠️ desktop **TTS 播放链路**(文本 → MiMo-TTS 音频 → 系统播放)未验证
- ✅ desktop 其余主流程(dashboard/memory/tasks/settings/chat)已验证 PASS
- ⚠️ mobile(app/) 从未在 shim 后端上验证

## 四、架构收敛评估(独立目录原则)

**现状**: shim 实现代码 **100% 在独立目录**;上游文件仅 13 个,改动全是 **env-gated opt-in 分支或枚举值**:

| 独立目录(我们的) | 上游文件 | 上游改动 |
|---|---|---|
| `firestore_pg/` | `database/__init__.py`, `_client.py` | 各 2-4 行 `FIRESTORE_PG_DSN` 分支 |
| `utils/auth_shim.py` | `utils/other/endpoints.py` | `AUTH_PROVIDER` 分支 |
| `utils/other/storage_minio.py` | `utils/other/storage.py` | `STORAGE_BACKEND` 分支 |
| `utils/cloud_tasks_redis.py` | `utils/cloud_tasks.py` | `QUEUE_BACKEND` 分支 |
| `utils/moss_pipeline/` | `utils/stt/pre_recorded.py` | `STT_PRERECORDED_MODEL=moss` 分支 |
| `utils/mimo_pipeline/` | `routers/tts.py` | `TTS_PROVIDER=mimo` 分支 |
| `utils/mimo_pipeline/`+`sensevoice/` | `utils/stt/streaming.py`+`receiver.py` | `STT_SERVICE_MODELS` 分支 + 枚举值 |
| — | `utils/llm/model_config.py`+`providers.py` | `TRANSLATION_PROVIDER`/`CHAT_PROVIDER` 配置 |
| — | `utils/translation_core/providers.py` | DeepSeek JSON fallback |

**可选收敛点**(减少上游 diff,但当前已是最小侵入):
1. `receiver.py` socket 创建 → 注册表模式(需在上游建工厂钩子,收益有限——当前 14 行分支与上游 Deepgram/Modulate 分支同构)
2. `providers.py` mimo 注册条目 → 保持(与上游 DeepSeek 条目同构)

**建议**: 保持现状。上游 diff 已最小化(全 env-gated),独立目录边界清晰,与上游 merge 兼容性已被 3 次零冲突验证。注册表重构反而偏离上游模式。

## 五、其他遗漏

- **大陆运营**: 需 ICP 备案、PIPL 隐私政策、支付通道(微信/支付宝)、短信登录、算法备案——见之前运营差距分析
- **MiMo 计费**: 平台 2026.1.26 起计费,`tp-` key(TokenPlan)已实测可用,注意配额
- **文档**: 生产迁移路线(OIDC/推送/登录/支付替换)未落文档
