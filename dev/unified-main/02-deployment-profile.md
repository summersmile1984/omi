# 02 · 统一部署 Profile：一份源、四端生成、两种后端同一契约

> 事实基线（2026-09-02 代码检视）：
> - `feature/cloud-neutral-shim` 已在四个面实现了"部署 profile"：Flutter `AppEnvironmentProfile.selfHosted` + `firebase_services_policy.dart`（`OMI_APP_PROFILE`/`OMI_AUTH_PROVIDER`/`OMI_FIREBASE_SERVICES_ENABLED`，185 处引用）；Windows `src/shared/deploymentProfile.ts`（`'omi_cloud'|'self_hosted'`，58 处）；macOS `DesktopDeploymentProfile`（`omiCloud|selfHosted`，未知值**收紧**为 selfHosted，143 处）；后端 `OMI_DEPLOYMENT_PROFILE` + `utils/identity.py`（`AUTH_PROVIDER`）+ `PUSH_PROVIDER` + `STORAGE_BACKEND` + `QUEUE_BACKEND` + `SELF_HOST_EGRESS_ALLOWLIST`，并新增不变量 `INV-DEPLOY-1`。**Web 客户端是唯一没有 profile 对象的端**（`web/app/src/lib/firebase.ts:31-52` 以 Firebase 配置是否存在为开关）。
> - `codex/cloudflare-adaptation` 的客户端改动很小但契约不同：移动端只有开发桥（`POST /auth-issue` + `OMI_AUTH_DEV_ISSUER_SECRET`，`!kReleaseMode`，过期时间客户端自定 `now+24h`，**无刷新**）；Auth Worker 用 Hono，basePath **`/api/better-auth`**（自托管 `auth-server` 是 **`/api/auth`**），JWT ES256 24h；Edge 不做 JWKS 校验（`BETTER_AUTH_JWKS_URL` 等声明了但未使用），而是每请求调 Auth Worker `/internal/verify` 并向下游签 60 秒 HMAC 断言头 `x-omi-auth-context`/`x-omi-internal-signature`；Web 用 vinext 跑在 Workers 上，cookie-only 会话，`/api/better-auth/*` 与 `/api/proxy/*` 同源代理，实时用 `POST /v1/realtime/web-ticket` 换 30 秒一次性票据。
> - 两个分支各自把"环境（prod/beta/local）"和"部署模型（托管云/自托管/Cloudflare）"混在一个枚举里，且四个枚举值名不一致（`production`/`omi_cloud`、`self_hosted`/`neutral`）。

## 0. 契约权威

**上游 API（OpenAPI 三份 + WebSocket 协议）是契约，自托管（上游单体 + shim）是参考实现，Cloudflare 单向对齐二者。** 客户端可见的路径、请求/响应形状、头、错误码、WS 首帧全部以上游为准；Cloudflare 内部的 Worker 间断言头、DO、D1、限流实现是它自己的事，不得反向进入共享契约。本文件里凡是"来自 CF 分支"的东西，只在不改变上游契约的前提下保留。

## 1. 模型：两个轴 + 一组派生值 + 一组能力开关

```
deployment_target ∈ { omi_cloud, self_hosted, cloudflare }     # 部署模型（后端实现 + 身份提供方）
stage             ∈ { production, beta, local }                  # 环境（URL、日志级别、允许生产数据）
identity_provider = omi_cloud → firebase ; self_hosted|cloudflare → better_auth      # 派生，不可单独设置
brand             = brand/<id>/manifest.yaml                     # 来自白牌层，提供域名与显示名
```

一条 **profile 记录** = `(target, stage, brand)` 解析后的全部值，四端与后端读到的是**同一份生成表**：

| 字段组 | 字段 | 说明 |
|---|---|---|
| 身份 | `identity_provider`、`auth_base_url`、`jwks_url`、`jwt_issuer`、`jwt_audience`、`jwt_ttl_seconds`、`social_providers[]` | Better Auth 契约见 §3 |
| 端点 | `api_base_url`、`ws_base_url`、`mcp_base_url`、`share_base_url`、`objects_public_url`、`update_feed_url`、`analytics{host,key_ref}` | 全部为 origin（无路径），release 强制 https，`canonicalSelfHostedOrigin` 语义保留 |
| 数据面（仅后端） | `data_plane ∈ {firestore, firestore_pg, d1}`、`object_store ∈ {gcs, minio, r2}`、`queue ∈ {cloud_tasks, redis, cf_queues}`、`vector ∈ {pinecone, qdrant, vectorize}`、`cache ∈ {redis, do_kv}` | 见 `03-deploy-targets.md` |
| 推送 | `push_provider ∈ {firebase, webhook, disabled}` | 品牌自有 FCM 项目也算 `firebase`（FCM 不要求 Firebase Auth） |
| 能力开关 | `allow_direct_model_providers`、`allow_byok`、`allow_google_connectors`、`allow_cloud_connectors`、`marketplace_enabled`、`stt_providers[]`、`tts_provider`、`realtime_relay`、`model_downloads`、`embedding_dims`、`sync_upload_batch_limit`、`max_request_bytes`、`account_activation_fence` | 取代今天散落的围栏；每个开关都有明确默认值表（§4） |
| 出站策略 | `egress_allowlist[]`、`egress_denylist[]` | 自托管：只允许运营方端点；Cloudflare：Cloudflare 服务 + 运营方端点；托管云：无限制 |

**规则**：① 未知 `target` 一律解析为最严格的 `self_hosted`（沿用 macOS 的 fail-restrictive）；② `identity_provider` 与 `target` 不配对即启动失败（沿用 shim 的 `validateIdentityConfiguration`）；③ 生产族标识（正式 Bundle ID/包名）**只接受构建期烘焙的 profile**，环境变量覆盖只对开发标识生效（沿用 `BundleEnvironment.swift:9-14` 与 `DesktopBackendEnvironment.pythonBaseURL` 的安全立场）；④ `omi_cloud` 只用于上游等价回归，fork 不发布。

## 2. 单一事实源与生成物

```
deploy/profiles/
├── schema.json
├── omi_cloud.yaml            # 上游值（api.omi.me / based-hardware …），只做回归
├── self_hosted.yaml          # 数据面/队列/推送/能力开关的目标默认值；URL 由 brand 域名 + stage 模板化
├── cloudflare.yaml
└── stages.yaml               # production/beta/local 的差异（允许 http、日志、允许生产数据…）
scripts/profiles/
├── render.py                 # (target, stage, brand) → 下列生成物；幂等
└── check_tables.py           # 四端生成表逐字段一致；是 checks-manifest.fork.yaml 的 fork-profile-consistency
```

| 端 | 生成物 | 接入方式（一次性改动） |
|---|---|---|
| Flutter | `app/lib/env/deployment_profiles.g.dart`：`DeploymentTarget` 枚举 + `DeploymentProfile` 记录表；`OMI_APP_PROFILE` 语义改为 `<target>.<stage>`（如 `self_hosted.production`），兼容现有 `production/mobile_beta/local_prod/local_dev` 作为 `omi_cloud.*` 的别名 | `environment_profile.dart` 的 `defaultApiBaseUrl/firebaseProjectId/authCallbackScheme` 改为查表；`firebase_services_policy.dart` 改为读 `identity_provider`；`env.dart` 三个校验器保留、比较对象改为表值；`managedClientValue` 改为 `profile.isManaged`；**Firebase 围栏不改调用点**：`app/pubspec_overrides.yaml`（新文件）把 `firebase_auth`/`firebase_messaging`/`firebase_crashlytics`/`firebase_core` 指向 `fork/packages/*_shim`（同名公开 API，Better Auth / webhook / no-op 实现），上游 139 处调用零改动（`00-upstream-touch-policy.md` §3） |
| macOS | `Desktop/Sources/DeploymentProfiles.generated.swift` + 构建期写入 `Info.plist` 的 `OMIDeploymentProfile` 键 | `DesktopDeploymentProfile` 增加 `.cloudflare`；`DesktopBackendEnvironment` 的四个 URL 常量改为查表；`DesktopModelEgressPolicy` 读能力开关 |
| Windows | `src/shared/deploymentProfiles.generated.ts` | `deploymentProfile.ts` 的 `DeploymentProfile` 增加 `'cloudflare'`；`loadWindowsDeploymentConfig` 先查表再允许 `VITE_*` 覆盖（仅 `stage=local`） |
| Web | `web/app/src/lib/deploymentProfile.generated.ts` + 运行时 `window.__DEPLOYMENT_PROFILE__` | **新建** profile 对象（今天没有）：`firebase.ts` 的 `isFirebaseAuthConfigured` 改为 `profile.identityProvider === 'firebase'`；`NEXT_PUBLIC_AUTH_MODE` 退役 |
| 后端（自托管） | `backend/.env.<stage>.self_hosted`（`OMI_DEPLOYMENT_PROFILE`、`AUTH_*`、`STORAGE_BACKEND`、`QUEUE_BACKEND`、`PUSH_PROVIDER`、`SELF_HOST_EGRESS_ALLOWLIST`、`CAPABILITIES_JSON`） | `env_loader.py` 的 stage 文件名规则扩展为 `.env.<stage>.<target>`（回退到现有 `.env.<stage>`）；`utils/identity.py` 不变 |
| 后端（Cloudflare） | 各 Worker `wrangler.<stage>.jsonc` 的 `vars` 段 + `deploy/cloudflare/manifests/profile.<stage>.json` | Edge/Auth/Python Workers 读同一 `vars` 键名（与自托管 env 名一致） |

`check_tables.py` 把每个生成物反解析成字段字典，与 `render.py` 的中间表逐字段比较；任何端手改生成文件即失败。

## 3. 身份契约 v1（两种后端必须相同，客户端只有一份实现）

| 项 | 契约 | 自托管现状 | Cloudflare 现状 → 改动 |
|---|---|---|---|
| 路径前缀 | **`/api/auth`**（Better Auth 默认；Edge 把 `/api/auth/*` 路由到 Auth Worker） | `/api/auth` ✅ | `/api/better-auth` → 改 basePath（Hono 一行）+ Web 代理路径 |
| 登录 | Better Auth 原生：`POST /api/auth/sign-in/email`、`sign-up/email`、social（Google/Apple）；移动端 bearer 插件，会话令牌在 `set-auth-token` 头 | `better_auth_client.dart` ✅（`:46,54,71`） | 开发桥 `/auth-issue` 降级为 `stage=local` 专用；移动端改用 shim 的客户端 |
| JWT | ES256；claims `sub`=`uid`、`uid`、`iss`、`aud`、`exp`；TTL 由 profile `jwt_ttl_seconds` 决定（默认 3600） | 15m（`auth.js:141`）→ 改读配置 | 24h（`index.ts:211-231`）→ 改读配置；`iss`/`aud` 必须设置并被校验 |
| 换取/刷新 | `GET /api/auth/token`（`Authorization: Bearer <sessionToken>`）→ `{token}`；客户端在 `exp-5min` 前用会话令牌换新 JWT | ✅（`better_auth_client.dart:94`；`shared.dart:60-107`） | 客户端沿用 shim 实现；`now+24h` 自造过期删除 |
| JWKS | `GET /api/auth/jwks`；轮换 30d/宽限 2d | ✅ | ✅（路径随 basePath 改） |
| 服务端校验 | **JWKS 本地校验**（`iss`/`aud`/`exp`/时钟偏移） | `auth_shim.py` ✅ | Edge 改为 JWKS 校验 bearer JWT（已声明 `BETTER_AUTH_JWKS_URL/ISSUER/AUDIENCE`，接上即可）；`/internal/verify` 只保留给 cookie 会话；Worker 间 HMAC 断言保留为 CF 内部实现细节，**客户端不可见** |
| Web 会话 | cookie-only；同源代理 `/api/auth/*`（剥离 JSON 里的会话令牌）与 `/api/proxy/*`（`credentials: include`） | 需新增（Web 尚无 profile） | ✅（`auth-proxy.ts`）→ 提炼为与运行时无关的代理模块，自托管 Next 服务端复用 |
| Web 实时 | **沿用上游** `/v4/web/listen` 协议：首帧 `{type:"auth", token, …}`，token 为 Better Auth JWT（Web 端用 cookie 会话调 `GET /api/auth/token` 换取） | 无需新增路由 ✅ | 删除 web-ticket 与 bootstrap 头改造；Edge 对首帧 JWT 做 JWKS 校验 |
| 移动端实时 | `/v4/listen`、`/v1/omni/relay` 直接 bearer JWT | ✅ | ✅ |
| Firebase 身份导入 | scrypt 校验 + 首次登录重哈希 | `firebase-migration-password.js` ✅ | `index.ts:142-175` ✅ → 抽到共享包 `auth/shared/`（TS）供两处 import |
| 账户激活围栏 | 上游契约里没有这个状态。它是 Cloudflare 内部 `omi_cloud→cloudflare` 迁移工具的状态，**不得成为客户端可见契约**；新品牌默认关闭 | 无 | `cloudflareProductTrafficDenial` 读 profile 开关，默认关闭；迁移期只在迁移工具内部使用 |

这样 `auth_service.dart`/`better_auth_client.dart`、`web/app/src/lib/api.ts`、`desktop/**/AuthService` 各只有一份；差异全部在服务端目录里。

## 4. 能力开关默认值表（进 `deploy/profiles/*.yaml`）

| 开关 | omi_cloud | self_hosted | cloudflare | 来源/说明 |
|---|---|---|---|---|
| `allow_direct_model_providers` / `allow_byok` | true | false | false | Windows `deploymentProfile.ts:207-210`、macOS `DesktopModelEgressPolicy` |
| `allow_google_connectors` / `allow_cloud_connectors` | true | false | true（经 Jobs Worker，`gmail-calendar-oauth-migration.md`） | CF 分支已把日历读取走 Worker（`CalendarReaderService.swift`） |
| `marketplace_enabled` | true | false（首发） | false（首发） | 白牌决策；见 `omi-white-label-strategy.md` |
| `stt_providers` | 上游策略 | `[operator_mimo, operator_moss, sensevoice_local]` | `[workers_ai_whisper, operator_*]` | `stt_provider_policy.py` 注册表化后按 profile 过滤 |
| `tts_provider` | elevenlabs | `mimo`/`sherpa` | `workers_ai_aura` | `tts_provider.py:72` |
| `realtime_relay` | openai | `operator_realtime` | `realtime_worker` | `OMI_REALTIME_MODEL_PROVIDER` |
| `embedding_dims` | 3072 | 1536/3072（Qdrant 均可） | **≤1536**（Vectorize 上限） | CF 计划书 §1.1 |
| `sync_upload_batch_limit` | 5 | 5 | **2** | 客户端默认与上游相同（5）；只有 `cloudflare` profile 因 100MB 请求上限降到 2（`local_wal_sync.dart:31` 的 CF 硬编码改为读表） |
| `max_request_bytes` | — | 由 nginx 决定 | 100 MB | 同上 |
| `push_provider` | firebase | `webhook`（或自有 FCM） | `webhook`（或自有 FCM） | `config/push_provider.py` |
| `account_activation_fence` | n/a | false | false（迁移期 true） | §3 |
| `model_downloads` | true | false | false | `env.dart:142-143` |
| 分析/崩溃上报 | PostHog/Sentry 上游 | 品牌自有或关闭 | 品牌自有或关闭 | `managedClientValue` → `profile.analytics` |

## 5. 迁移映射（两分支现有代码 → 统一实现）

| 现有代码 | 处置 |
|---|---|
| shim：Flutter `AppEnvironmentProfile.selfHosted`、`firebase_services_policy.dart`、`better_auth_client.dart`、`better_auth_session_store.dart`、`shared.dart` 刷新逻辑、`canonicalSelfHostedOrigin` | **保留为统一实现主体**；枚举值改查生成表；新增 `cloudflare` |
| shim：Windows `deploymentProfile.ts`、macOS `DesktopDeploymentProfile`/`DesktopModelEgressPolicy` | 保留；增加 `cloudflare`；URL 常量改查表 |
| shim：后端 `utils/identity.py`、`auth_shim.py`、`config/push_provider.py`、`storage.py`/`storage_minio.py`、`cloud_tasks_redis.py`、`egress_policy.py`，以及被内联进上游文件的 MiMo/MOSS/SenseVoice/TTS provider | 保留逻辑，**全部迁入 `backend/fork/`**，通过 `backend/fork/main.py` 入口与导入时补丁注册表挂入，上游后端文件恢复零改动；`OMI_DEPLOYMENT_PROFILE` 值统一为 `omi_cloud/self_hosted/cloudflare`（`neutral` 作别名一版后删除） |
| shim：`web/app` Moonshine/Bun 重写（删除 Next 配置与 `src/app/**`，138 个新路由文件） | **不合入**（见 `03-deploy-targets.md` D3）；打 tag 归档 |
| shim：修改的 11 个上游不变量文档 + 新增 `INV-DEPLOY-1`、`INV-TASK-2` | 新增文件合入；对上游不变量的修改改写为 fork 增补文件 `docs/product/invariants/fork/*.md`，上游文件不动 |
| CF：Flutter 开发桥（`better_auth_session.dart`、`usesBetterAuth`、`_betterAuthDevBridgeEnabled`） | 仅保留 `stage=local` 的开发桥入口；其余由 shim 客户端取代 |
| CF：`local_wal_sync.dart:31` 批量 5→2 | 改为读 `profile.sync_upload_batch_limit` |
| CF：Auth Worker `basePath`、JWT TTL、`iss/aud`、Edge 的 `/internal/verify` 用于 bearer | 按 §3 改 |
| CF：Web vinext 构建、同源 Better Auth 代理、`AuthProvider.tsx` 双模式 | 保留；`NEXT_PUBLIC_AUTH_MODE` 改为读 profile；代理模块抽成运行时无关；**web-ticket 协议不保留**（改回上游 `/v4/web/listen` 首帧 token） |
| CF：`rate_limit_config.py` +12 策略、`export_openapi.py` 的 `cloudflare-route-inventory` 面、`test_openapi_contract.py` 三个用例、`checks-manifest.yaml` 三条检查 | **不改上游文件**：12 条策略放 `deploy/cloudflare/manifests/rate-limits.yaml`，fork 测试断言其 ⊇ 上游 `rate_limit_config.py`；route-inventory 生成器移到 `deploy/cloudflare/scripts/route_inventory.py`（import 上游 app 与 `export_openapi.py` 的 hermetic bootstrap 函数，不修改该脚本）；三个用例进 fork 测试目录；三条检查进 `checks-manifest.fork.yaml` |
| CF：`CalendarReaderService.swift` 走 Worker、`APIClient+Calendar.swift` | 合入，但按 `allow_google_connectors` 与 `target` 选择实现（自托管无 Jobs Worker → 该能力关闭） |

## 6. 守卫（进 `checks-manifest.fork.yaml` 与各端测试）

- `fork-profile-consistency`：四端生成表与 `render.py` 中间表逐字段相等。
- 契约测试（三端各一份，读同一夹具 `contracts/profile/fixtures/*.json`）：未知 target → `self_hosted`；`self_hosted|cloudflare` 且 `identity_provider=firebase` → 启动失败；`omi_cloud` 且 `better_auth` → 启动失败；release 且非 https origin → 失败；`omi_cloud` 表值与上游字面量一致（等价回归）。
- 身份契约套件 `contracts/auth/`：对任一目标的 Auth 服务跑同一组 HTTP 用例（sign-up → sign-in → `set-auth-token` → `/token` → JWKS 校验 → 过期刷新 → sign-out → Web cookie 流 → cookie 换 JWT → `/v4/web/listen` 首帧鉴权）；`fork-contract-selfhost.yml` 与 `fork-contract-cloudflare.yml` 都跑。
- 现有守卫保留：shim 的 `app/test/unit/env_test.dart`（55 处 profile 断言）改为读生成表；Windows `deploymentProfile.test.ts`；macOS `DesktopBackendEnvironment` 测试。

## 7. 与白牌层的边界

品牌层只提供**值**（域名、显示名、Bundle ID、颜色、文案）；profile 层提供**行为**（身份提供方、数据面、开关）。`render.py` 读 `brand/<id>/manifest.yaml` 的 `domains.*` 来填 profile 的 URL 字段；`apply.py` 读 profile 结果来渲染 xcconfig/plist/wrangler vars。两者的生成顺序：先 profile（行为）再 brand（值），任一变更都重跑两个 check。
