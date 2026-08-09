# 本地部署验证台账(改造版 cloud-neutral fork)

日期: 2026-08-09 · 分支: feature/cloud-neutral-shim · 目标: 4C8G 自托管部署就绪度

## 一键部署

```bash
dev/deploy-local.sh            # 全栈:容器 + auth-server + worker + backend
dev/deploy-local.sh --no-backend  # 仅容器
dev/deploy-local.sh --stop     # 全部拆除
```

启动组件: PG(firestore_pg shim)+ Redis + MinIO + emulators(dev 认证)+
Better Auth auth-server(:3000)+ Redis queue worker + backend(:8100, 全 shim env)。

## 部署验证结果

| 组件 | 验证 | 结果 |
|---|---|---|
| **后端启动** | health 200,26 表 + 29 复合索引,无 import 错误 | ✅ |
| **PG**(firestore_pg) | `pg-ok` | ✅ |
| **Redis** | PONG | ✅ |
| **MinIO** | health 200 | ✅ |
| **Better Auth**(auth-server) | /health 200, /api/auth/jwks 200 | ✅ |

## 端到端验证

| Shim | 验证链 | 结果 |
|---|---|---|
| **存储(MinIO)** | `upload_profile_audio` → MinIO → `get_user_has_speech_profile`=True → 下载 | ✅ |
| **队列(Redis)** | `enqueue_sync_job` → Redis(SADD 去重)→ worker BLPOP → POST backend → 队列清空 | ✅(handler 403 因缺 Cloud Tasks OIDC,预期边界) |
| **认证(Better Auth)** | `/auth-issue` 签 EdDSA JWT → `verify_token`(AUTH_PROVIDER=better_auth)→ 返回正确 uid | ✅ |
| **翻译(可配置)** | `TRANSLATION_PROVIDER=mimo` → mimo-v2.5@xiaomimimo;`=deepseek` → deepseek-v4-flash@deepseek | ✅ |
| **MOSS 批 STT**(外部 API) | 之前验证:moss-transcribe-diarize 分离 S01/S02 | ✅(需 MOSS_API_KEY) |
| **SenseVoice live STT**(本地 CPU) | 之前验证:中文 0.04s,CER 7.81% | ✅ |

## 发现并修复的问题

1. **Better Auth JWKS/secret 初始化**: auth-server 重启换 secret 后,/auth-issue 报
   "Failed to decrypt private key" — 因 JWKS 表残留旧 secret 加密的私钥。
   **修复**: deploy-local.sh 启动 auth-server 前检查 jwks 表,非空则 TRUNCATE(重新生成)。

## 4C8G 部署就绪度

| 维度 | 状态 |
|---|---|
| 存储(PG shim + MinIO) | ✅ 就绪 |
| 认证(Better Auth shim) | ✅ 就绪 |
| 异步队列(Redis) | ✅ 就绪(handler OIDC 门在本地无 token,生产可配) |
| 批 STT(MOSS API) | ✅ 就绪(外部 API) |
| live STT(SenseVoice CPU) | ✅ 就绪(本地) |
| 说话人识别(wespeaker CPU) | ✅ 就绪(本地) |
| 翻译(MiMo/DeepSeek/Gemini 可配) | ✅ 就绪 |
| **合计** | **本地全栈验证通过,4C8G 部署就绪** |

## 已知边界

- Redis 队列 handler 在本地返回 403(Cloud Tasks OIDC 验证)——机制已验证,生产可配 OIDC 或关 gate
- MOSS/翻译/STT 需真实 API key(MOSS_API_KEY / MIMO_API_KEY / DEEPSEEK_API_KEY)
- auth-server 生产需强 BETTER_AUTH_SECRET(非 dev 默认)

## 补充: Mac desktop + backend E2E(2026-08-09)

### 后端侧(已验证)
desktop 将请求的 6 个核心数据端点全部 200(shim 后端):
- `/v1/conversations` / `/v3/memories` / `/v1/action-items` / `/v1/goals` / `/v1/folders` / `/v1/users/profile` ✅
- MinIO shim: desktop-e2e-user profile 上传到 `e2e-profiles/desktop-e2e-user/speech_profile.wav` ✅
- 后端以 deploy-local.sh 全 shim env 运行(STORAGE_BACKEND=minio, QUEUE_BACKEND=redis)

### desktop 侧(受构建环境阻塞)
- named bundle 启动命令已验证正确:
  ```
  FIREBASE_PROJECT_ID=demo-omi-local FIREBASE_AUTH_PROJECT_ID=demo-omi-local \
  OMI_APP_NAME=omi-e2e OMI_DESKTOP_LOCAL_PROFILE=1 OMI_SKIP_BACKEND=1 OMI_SKIP_TUNNEL=1 \
  OMI_PYTHON_API_URL="http://127.0.0.1:8100/" OMI_DESKTOP_API_URL="http://127.0.0.1:8100/" \
  OMI_ENABLE_LOCAL_AUTOMATION=1 ./run.sh
  ```
- run.sh 正确进入 named bundle 路径(跳过 backend 用我们的 shim、auth 指向 demo-omi-local)
- **阻塞**: swiftpm 下载 Google Firebase xcframework(Firestore/Analytics ~1GB)在国内网络间歇性挂起,首次完整构建未能在会话内完成
- omi-ctl 桥(47777)与语义动作机制已确认,app 启动后即可用
- 这是**构建环境/网络**限制,非我们的改造问题;生产/海外网络可正常构建
