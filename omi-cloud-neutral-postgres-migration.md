# memweft Fork 云中立化改造 — 改动范围全景调研

> 目标:将 Omi/memweft 仓库 fork 改造成**云中立**(cloud-neutral,自托管可部署)系统,以 **PostgreSQL 作为主存储**。
> 本文档是调研交付物:现状云绑定全景 → 各层替代方案 → 逐模块改动清单 → 工作量与风险。

---

## 0. 结论先行

**这是全仓库级改造,不是局部替换。** 核心事实:

1. **主存储是 Firestore,不是 SQL**:`backend/database/` 有 **88 个模块**,全部基于 Firestore(`google.cloud.firestore`),含事务、查询、索引、加密、缓存多层抽象。
2. **认证是 Firebase**:移动端 `firebase_auth`、后端 `firebase_admin` 验 ID token、FCM 推送、Firestore security rules 全部绑定 Firebase。
3. **部署面是 GCP**:Cloud Run(backend/backend-sync/pusher 等)、GKE(agent-proxy、llm-gateway、diarizer、parakeet)、Cloud Tasks(5 条队列)、Modal GPU(VAD/说话人)、GCS 音频存储。
4. **好消息**:架构本身已做了端侧/服务侧分离(桌面端本地 SQLite + REST API,移动端纯 API 客户端),**两个客户端不用动数据库**;Redis 已是自托管友好;STT 的 parakeet/deepgram 已是自托管 GPU 服务。

**改造规模估算:4~6 人月**(后端数据层 60% + 认证/部署 25% + 客户端适配 15%),可分期:先 PostgreSQL 主存储(功能不变),再去 Firebase,最后去 GCP 部署面。

---

## 1. 现状云绑定全景

### 1.1 存储层(最重)

| 组件 | 当前实现 | 规模/使用面 | 云绑定度 |
|---|---|---|---|
| **主存储** | Firestore `users/{uid}/<collection>`(88 个 database 模块) | conversations(加密 segments)、memories、action_items、goals、chat、users、workstreams、candidates、staged_tasks、screen_activity… ~40 个集合 | 🔴 100% |
| 事务 | `firestore.transactional`(goals focus、action_items 挂靠、account deletion、candidate 审批…) | 10+ 事务边界,有 `firestore_transaction_retry.py` | 🔴 |
| 查询 | `FieldFilter` + 复合索引(需显式索引文件 `reconcile_firestore_indexes.py`) | 大量 where+orderBy+limit 查询 | 🔴 |
| 加密 | AES-256-GCM 每用户密钥,HKDF-SHA256 派生 | `database/helpers.py`、`utils/encryption.py` | 🟡 逻辑可复用 |
| 缓存 | Redis `database/redis_db.py`(fail-open) | 速率限制 Lua、fair-use 分钟桶、锁(listen/goal)、pub/sub | 🟢 已自托管 |
| 向量 | Pinecone `database/vector_db.py` | memories/action_items 向量检索 + repair outbox worker | 🔴 |
| 图谱 | Neo4j `database/knowledge_graph.py` | 记忆图谱、graph_tools | 🔴(可砍) |
| 文件 | GCS `google.cloud.storage`(private_cloud_queue) | 音频、photos、files | 🔴 |

### 1.2 认证与推送

| 组件 | 现状 | 位置 |
|---|---|---|
| 身份认证 | Firebase Auth(Google/Apple OAuth)→ ID token | 移动端 `firebase_auth`;后端 `dependencies.py`/`main.py` `verify_id_token` |
| Admin SDK | `firebase_admin.initialize_app`(SERVICE_ACCOUNT_JSON) | main.py、desktop_backend.py、agent-proxy、modal jobs |
| 推送 | FCM `firebase_messaging` | 移动端 notification_service_fcm.dart;后端 `database/notifications.py` + fcm_tokens 集合 |
| 崩溃上报 | Firebase Crashlytics | 移动端 |
| 远端配置 | Firebase Remote Config | 移动端 |

### 1.3 部署与作业

| 组件 | 现状 | 位置 |
|---|---|---|
| API 服务 | Cloud Run(backend、backend-sync、backend-sync-backfill、backend-listen) | `.github/workflows/gcp_*.yml` |
| 常驻服务 | GKE Helm charts(agent-proxy、diarizer、llm-gateway、nllb-translation、parakeet、pusher、vad、monitoring) | `backend/charts/` |
| 队列 | Cloud Tasks 5 条:`sync-jobs`、`sync-backfill`、`audio-merge`、`account-deletion`、`conversation-finalization` | services/conversation_finalization.py 等 |
| 定时作业 | Cloud Run Jobs / Scheduler(notifications-job、memory-maintenance-job、agent-vm-reaper) | `backend/modal/`、`backend/jobs/` |
| GPU | Modal(自托管部署用)+ 自托管 parakeet/deepgram/diarizer/nllb | `backend/modal/main.py` |
| Agent VM | GCE VM 生命周期(agent-proxy + reaper) | `backend/services/agent_vm_lifecycle.py` |

### 1.4 外部 API(保留或替换)

- LLM:OpenAI/Anthropic/Gemini(BYOK 已有)+ 自建 `llm-gateway`(云中立可选)
- STT:parakeet 自托管 ✅ / modulate(托管 API)/ deepgram 自托管 ✅ / soniox(移动端默认)
- Stripe 订阅、Twilio 电话、Todoist/MS To-Do 集成(第三方,不动)
- X connector、NPS、LangSmith 等外围

---

## 2. PostgreSQL 迁移方案

### 2.1 架构策略:repository 模式,不是 ORM 大爆炸

现有代码风格是"每 domain 一个 `database/<domain>.py` 模块,内部调 Firestore 客户端"。最省改动、最可验证的路径:

```
database/_client.py(get_firestore_client)
  → 新增 database/pg_client.py(asyncpg/SQLAlchemy 2.0 连接池)
  → 每个 <domain>.py 内部:Firestore 调用 → SQL 调用(保持函数签名不变)
  → 事务:firestore.transactional → asyncpg transaction / SQLAlchemy session
  → 查询:FieldFilter 链 → SQL WHERE/ORDER BY/LIMIT(复合索引 → PG index)
```

**关键决策**:
- 保留 `database/*.py` 模块边界,routers/utils 层零改动(它们是纯业务编排,不直接碰 Firestore)
- 用 **SQLAlchemy 2.0(async)+ Alembic 迁移**(社区成熟、CLI 生态好);pgvector 处理向量
- 兼容层:写一个 `FirestoreCompat` 适配(transactional 装饰器 + FieldFilter 语义)→ 老代码渐进迁移,每 domain 一个 PR

### 2.2 库表映射(核心集合 → PG schema)

```
users/{uid}/                          →  user 表(uid PK)+ 全局表(带 uid 列)
  ├── conversations                   →  conversations(id, uid, structured JSONB, encrypted_segments BYTEA)
  ├── memories                        →  memories(id, uid, content, category, vector vector(1536), …)
  ├── action_items                    →  action_items(id, uid, status, due_at, goal_id FK, workstream_id FK)
  ├── goals + progress_events         →  goals(id, uid, status, metric JSONB) + goal_progress_events
  ├── workstreams + events + artifacts→  workstreams / workstream_events / artifact_refs
  ├── candidates / staged_tasks       →  candidates(…, status, score)
  ├── chat_sessions / messages        →  chat_sessions / messages(text 加密)
  ├── users / people / folders        →  users / people / folders
  ├── screen_activity                 →  screen_activity(id, uid, ts, text, embedding)
  ├── notifications / fcm_tokens      →  notifications / device_tokens
  ├── sync_jobs / sync_content_ledger →  sync_jobs / sync_ledger
  ├── fair_use / usage / llm_usage    →  统计表(带唯一键,便于 UPSERT)
  └── …其余 ~20 个集合                 →  同类映射(JSONB 列兜底无 schema 的嵌套文档)
```

**JSONB 策略**:Firestore 文档天然无 schema,嵌套对象、数组、Map 字段一律 `JSONB` 列承载,只把**查询/排序/去重/外键**字段提为真实列。这能覆盖 90% 现有查询语义,不需要完全建模。

### 2.3 高难点的 Firestore 语义 → PG 等价

| Firestore 能力 | 使用处 | PG 等价 |
|---|---|---|
| 事务(读后写一致性) | goals focus(独占槽位)、action_items 挂靠校验、candidate 审批、account deletion | `BEGIN … SELECT … FOR UPDATE` / SQLAlchemy session 事务 ✅ |
| 复合索引(where+orderBy) | action_items 日期分页、conversation 列表、screen_activity 搜索 | 普通 B-tree/BRIN 复合索引 ✅ |
| 数组包含(ArrayContains) | 标签/分类查询 | `@>` jsonb / 关联表 ✅ |
| 集合组查询(collectionGroup) | memories 全用户聚合、审核队列 | `uid` 列 + 索引 ✅ |
| 宽松写入(无 schema) | 全部写入路径 | JSONB 兜底 ✅ |
| 加密 segments(只读 blob) | conversations.segments | BYTEA(加密逻辑不变)✅ |
| 多租户隔离(`users/{uid}/`) | 全部 | 每条业务行带 uid 列 + 行级策略(RLS 可选)✅ |
| 文档级粒度锁 | delete/batch 竞态 | `SELECT … FOR UPDATE` / advisory lock ✅ |
| 游标分页(doc id 排序) | 大量列表页 | `(created_at, id)` keyset pagination ✅ |
| 实时监听(仅移动端直连) | 移动端 Firestore 直读(如有) | 需改 REST 轮询/SSE(见 §4)⚠️ |

### 2.4 向量检索:Pinecone → pgvector

- `vector_db.py` 的 Pinecone 调用 → `pgvector`(`vector(1536)` 列 + HNSW 索引)
- 现有 `memory_vector_repair_outbox_worker`、`vector_repair_pinecone_adapter` 改造为 PG 维护 worker
- 需验证:1536 维 embedding(OpenAI 默认)在 PG HNSW 下的查询延迟(<50ms 目标);大规模(>1M 向量)可换 pgvector 分区或 Qdrant(云中立,S3/MinIO 存储)

### 2.5 文件存储:GCS → S3 兼容

- `storage.Client` → boto3 / minio SDK(S3 API)
- 自托管 MinIO 或本地磁盘路径(配置驱动),音频/照片/文件全部走同一抽象
- 现状已有 `private_cloud_queue` 抽象,改造面可控

### 2.6 图谱:Neo4j → PG 或砍掉

- `knowledge_graph.py` + graph_tools + graph_enrichment:记忆实体关系图谱
- 选项 A(推荐):映射为 `(source_id, target_id, relation)` 边表 + GIN 索引
- 选项 B:砍掉图谱,只保留记忆引用(`evidence_refs` JSONB)——产品影响需评估

---

## 3. 认证与推送替换

### 3.1 认证(第二大改造)

```
现状:Firebase Auth(Google/Apple OAuth)→ ID token → verify_id_token
目标:自托管 OIDC:
  - 方案 A(推荐):直接自托管 OIDC 提供商(Authelia / Keycloak / Zitadel / logto)
    → 移动端换 OIDC 流程,后端 verify_id_token → 验 JWKS
  - 方案 B:自建最小认证(FastAPI + pyjwt):Google/Apple OAuth 回调已在 routers/auth.py,
    改为自己签发/验证 JWT;但 Apple Sign-In 需要自有 Apple developer 资质
  - 方案 C(最小改动):保留 Firebase Auth 作为身份提供者(用户可选),
    新增自托管认证为默认——双轨并存
```

- 后端所有 `Depends(get_current_user_uid)` / `auth.with_rate_limit` 只依赖"验 token 出 uid",抽象已隔离 → 换验签逻辑即可
- `ADMIN_KEY` 机制已存在(本地/运维直通),可升级为服务间 mTLS/共享密钥

### 3.2 推送:FCM → 自托管

- 方案 A:UnifiedPush(自托管,安卓生态)/ APNs 直连(苹果仍需开发者账号)
- 方案 B:接入第三方推送网关(OpenPush、ntfy)→ 移动端 `notification_service_fcm.dart` 换实现
- 后端 `notifications.py` + fcm_tokens 已抽象,换 provider 即可;`notifications-job` 保留

---

## 4. 客户端改动范围(比想象小)

### 4.1 移动端(Flutter)

| 依赖 | 改动 |
|---|---|
| `firebase_auth` | 换 OIDC 登录(Google 保持原生、Apple 保持原生,后端换验签) |
| `firebase_messaging` | 换自托管推送 provider |
| `firebase_crashlytics` | 可换 Sentry(自托管)或移除 |
| Firestore 直读(如有) | 全部走 REST API(现状 95% 已走 API) |
| `Env.apiBaseUrl` | 指向自托管域名,零代码改动 |

### 4.2 桌面端(macOS/Windows)

- **已是云中立架构**:本地 SQLite(57 个源文件引用,任务/记忆/观察/agent 状态)+ REST API 客户端 + keychain 存 token
- 改动点:① `BundleEnvironment.swift` 生产环境密钥覆盖逻辑(换自托管 URL/密钥);② 登录流(Google/Apple OAuth → 自托管 OIDC);③ agent daemon 的 `api.omi.me` 网关路由改为自托管(pi-mono 适配器已是配置驱动)
- 桌面截屏/OCR/embedding 全部本地,不受影响

### 4.3 固件/设备端

- BLE 协议、音频编码(`sdks/device/PROTOCOL.md`)与云端无关,零改动
- 唯一注意:设备 OTA 更新通道(desktop_update_channels)需指向自托管分发

---

## 5. 部署面云中立化

| 现状 | 替代 | 改动 |
|---|---|---|
| Cloud Run | 任意容器平台:docker compose / K8s / 裸机 systemd / Nomad | Dockerfile 已齐备,换 CI 目标即可 |
| GKE Helm charts | 同一套 Helm 部署到自托管 K8s 或 compose | charts 复用,去掉 GCP 专属 Ingress/ILB |
| Cloud Tasks 5 队列 | Redis RQ / Celery / Arq / 自建 job 表 + 轮询 | `services/conversation_finalization.py`、`utils/sync/pipeline.py` 改调度器 |
| Cloud Run Jobs + Scheduler | cron + docker(cronjob) | 脚本已是独立入口,包一层 cron |
| Modal GPU(VAD/说话人) | 直接部署 Modal 镜像到自托管 GPU 机 | `modal/main.py` 是 FastAPI,可独立跑 |
| GCE agent VM | 自托管 VM(OpenStack/libvirt/docker 内)或砍掉 agent-proxy(桌面 agent 本地) | agent-proxy 是手机↔VM 桥,可标记可选 |
| Secret Manager/ExternalSecret | Docker secrets / SOPS / Vault | `backend-secrets` chart 换源 |
| GCS | MinIO/S3(见 §2.5) | — |
| Monitoring(普罗米修斯已自托管) | ✅ 无需改 | — |

---

## 6. 分期实施路线

```
Phase 1(2~2.5 月)— PostgreSQL 主存储,行为不变
  P0:pg_client 基建 + Alembic + 核心 6 域迁移(users/conversations/memories/
     action_items/goals/chat)+ 事务/查询/分页适配 + 加密 segments
  P1:剩余 30+ 域迁移(JSONB 兜底)+ Pinecone→pgvector + GCS→MinIO
  P2:双写影子模式跑一周,数据校验脚本,切换

Phase 2(1~1.5 月)— 去 Firebase
  P0:自托管 OIDC + 后端验签替换 + ADMIN_KEY 强化
  P1:推送 provider 替换 + Crashlytics→Sentry + 移动端登录改造

Phase 3(1~1.5 月)— 部署面云中立
  P0:Cloud Tasks→Redis 队列 + Cloud Run→自托管容器 + 5 条队列逐一迁移
  P1:cron 作业迁移 + agent-proxy 可选化 + CI/CD 工作流重写(gcp_*.yml→自托管 runner)

Phase 4(0.5 月)— 收尾
  文档、备份/恢复(Wal-g)、监控告警、扩容演练
```

---

## 7. 风险与注意点

1. **Firestore 事务语义是最大雷区**:现有 10+ 事务边界(goals focus 独占槽位、account deletion wipe、candidate 审批)依赖文档级读后写一致性——PG 必须用 `SELECT FOR UPDATE` 逐一对齐,并有并发测试(现有 `StrictFirestore` 测试基建可复用)
2. **加密字段**:conversations.segments 加密是应用层(AES-256-GCM),PG 迁移不变,但要验证 BYTEA 存储与密钥轮换逻辑
3. **Firestore 宽松 schema 的隐藏假设**:迁移脚本要扫描所有写入路径,JSONB 兜底会掩盖类型错误;建议加 PG CHECK 约束 + 现有 Pydantic 模型作为校验层
4. **移动端已发布版本兼容**:`docs/api-reference/app-client-openapi.json` 兼容边界——API 层零改动可避免
5. **parakeet/deepgram 已自托管**:STT 云中立度已经很高,modulate/soniox 是可选供应商,可配置降级
6. **Apple 生态硬依赖**:Apple Sign-In、APNs、App Store 分发无法完全云中立,需保留 Apple developer 账号(这是"云中立"的现实边界)

---

## 8. 改动文件清单(按工作量排序)

| 面积 | 文件 | 工作量 |
|---|---|---|
| 主存储 | `backend/database/` 88 模块(核心:users/conversations/memories/action_items/goals/workstreams/candidates/chat/screen_activity/notifications/sync_jobs/fair_use),45+ Firestore 集合 | 🔴 55% |
| 存储基建 | `database/_client.py`、`read_boundary.py`、`helpers.py`、`firestore_transaction_retry.py`、`firestore_cache*.py`、`vector_db.py`、`memory_vector_repair_*`、`knowledge_graph.py` | 🔴 15% |
| 认证 | `dependencies.py`、`utils/other/endpoints.py`、`routers/auth.py`、`desktop_backend.py`、`agent-proxy/main.py`、移动端 auth 服务 | 🟡 10% |
| 作业/队列 | `services/conversation_finalization.py`、`utils/sync/pipeline.py`、`routers/transcribe.py`(finalization)、`services/users/account_deletion.py`、`backend/modal/job.py`、`backend/jobs/*` | 🟡 10% |
| 部署 | `.github/workflows/gcp_*.yml`、`backend/charts/`、`runtime_images.json`、`deploy/runtime_env.yaml` | 🟡 8% |
| 客户端 | 移动端 auth/推送服务、桌面 `BundleEnvironment.swift`、agent daemon 网关路由 | 🟢 2% |
| 文件存储 | GCS 使用点(utils/other/storage、private_cloud_queue、jobs/agent_vm_reconciler) | 🟢 <1% |

---

## 9. 一句话总结

**云中立化的核心工作量不在"换数据库",而在"把 88 个 Firestore 模块的查询/事务语义无损搬到 PG + 自托管 OIDC/推送/队列"。** 好消息是架构分层(端侧独立、REST API 边界、Redis 已自托管、STT 已自托管)让客户端和外围服务几乎不用动;推荐以"PG 主存储先行、双写影子验证、逐域迁移"的方式推进,避免大爆炸式重写。
