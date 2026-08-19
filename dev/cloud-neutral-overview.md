# Cloud-Neutral 自托管改造总览

日期: 2026-08-10 · 分支: feature/cloud-neutral-shim · 目标: 4C8G 无云无 GPU 自托管部署

## 现状:端到端全绿 ✅

移动端 + 桌面端都已对接 fork 后的自托管服务,local dev 环境完整跑通。

```
┌─────────────┐  ┌─────────────┐
│  移动端 app  │  │  桌面端 app  │
│ Android 模拟 │  │ omi-e2e-verify│
└──────┬──────┘  └──────┬──────┘
       │ 10.0.2.2:8100  │ 127.0.0.1:8100
       └────────┬───────┘
                ▼
      backend :8100 (AUTH_PROVIDER=better_auth)
      认证: auth-server :3000 (JWKS)
      ┌──────────┬──────────┬──────────┬───────────┐
      │          │          │          │           │
      ▼          ▼          ▼          ▼           ▼
   firestore_pg  MinIO      Redis      MiMo       DeepSeek
   (PostgreSQL) (存储)      (队列)   (STT/TTS)  (聊天/翻译)
```

## 组件状态

### 后端 shim(全独立目录,上游只改配置)
| Shim | 目录 | 上游文件 | 启用 |
|---|---|---|---|
| Firestore→PG | `firestore_pg/` | `database/__init__.py`+`_client.py` | `FIRESTORE_PG_DSN` |
| Firebase Auth→BetterAuth | `utils/auth_shim.py` + `auth-server/` | `utils/other/endpoints.py` | `AUTH_PROVIDER=better_auth` |
| GCS→MinIO | `utils/other/storage_minio.py` | `utils/other/storage.py` | `STORAGE_BACKEND=minio` |
| Cloud Tasks→Redis | `utils/cloud_tasks_redis.py` | `utils/cloud_tasks.py` | `QUEUE_BACKEND=redis` |

### AI 功能(无 GPU,全真实可用)
| 功能 | Provider | 状态 |
|---|---|---|
| live STT | MiMo-V2.5-ASR(`mimo-v2.5-asr`) | ✅ 真实转写 |
| 批 ASR | OpenMOSS | ✅ 分离 S01/S02 |
| TTS | MiMo-V2.5-TTS(`mimo-v2.5-tts`) | ✅ 桌面端播报确认 |
| 翻译 | DeepSeek | ✅ 中译 |
| 聊天 | DeepSeek(Anthropic 兼容) | ✅ |

### 端到端验证
| 端 | 认证 | 数据 | AI |
|---|---|---|---|
| 移动端 | BetterAuth(UI 按钮) | ✅ 200 | STT/TTS 通 |
| 桌面端 | BetterAuth(env 注入) | ✅ 200 | chat 200×11 + TTS 播报 200×11 |

## 关键配置(dev/deploy-local.sh 已固化)

```bash
AUTH_PROVIDER=better_auth        AUTH_JWKS_URL=http://127.0.0.1:3000/api/auth/jwks
STORAGE_BACKEND=minio            MINIO_ENDPOINT=http://127.0.0.1:9000
QUEUE_BACKEND=redis              REDIS_DB_HOST=127.0.0.1
STT_SERVICE_MODELS=sensevoice|mimo   SENSEVOICE_MODEL_DIR=/tmp/sherpa/...
TTS_PROVIDER=mimo                MIMO_API_KEY=<tp- key>  MIMO_USE_TOKENPLAN=1
TRANSLATION_PROVIDER=deepseek    CHAT_PROVIDER=deepseek
```

## 参考文档
- `dev/e2e-verification-report.md` — 两端端到端验证 + 播报确认
- `dev/mobile-validation-ledger.md` — 移动端构建/运行/登录
- `dev/desktop-main-flow-ledger.md` — 桌面端主流程
- `dev/omi-external-callable-audit.md` — 外部依赖可调用性审计
- `dev/omi-gap-analysis.md` — 遗漏分析 + 生产迁移路线
- `dev/deploy-local-ledger.md` — 一键部署台账

## 遗留(生产迁移,未做)
- FCM 推送 / Google-Apple OAuth / Stripe 支付(大陆运营需替换)
- Pinecone / Neo4j(env-optional,无 key 跳过)
- 移动端 WebSocket 录音到 pusher 未验证
