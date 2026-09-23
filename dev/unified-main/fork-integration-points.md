# Fork 如何接管上游基础设施：现行代码入口

本页按**实际执行路径**记录接管点，不把早期规划当作已交付功能。总体策略是保留上游业务代码及其调用者；部署时选择 fork 入口，在上游代码加载前替换依赖边界，或先复制源码到独立构建目录再覆盖指定文件。`omi_cloud`、`self_hosted`、`cloudflare` 是不同部署 profile，**Cloudflare Worker 不运行自托管 Python 的补丁注册表**。唯一获准修改的上游源文件及其额度以 [白名单](upstream-touch-allowlist.yaml) 为准；接缝原则见 [fork 规则](../../AGENTS.fork.md)。

## 共同选择器：profile、品牌和目标

- [deploy/profiles/](../../deploy/profiles/) 是目标和能力配置源；[scripts/profiles/render.py](../../scripts/profiles/render.py) 的 `resolve` 校验目标、阶段、品牌、端点、身份和数据平面，渲染 Dart、Swift、Web 以及后端的 fork 专属表。Windows 不消费该表，而由自身 staging 生成 native profile。生成表的路径和模式见该文件开头；**仓库内的后端生成表不是自托管镜像的配置证明**：镜像构建时按目标重新渲染。
- [backend/fork/profile.py](../../backend/fork/profile.py) 的 `current` 从生成表及环境选择 profile；未显式选择时是 `omi_cloud.production`。[backend/fork/bootstrap.py](../../backend/fork/bootstrap.py) 只在 `self_hosted` 路径装入 Python 适配器；`cloudflare` 在这里被拒绝，不会误用 PostgreSQL 补丁。
- 品牌资源/标识和后端选择是不同维度；profile 决定连接哪个 API/Auth/Web 端点及开放哪些能力，不代表某个尚未迁移的后端接口已经可用。

## 自托管后端：加载上游前接管依赖

调用链：[deploy/self-host/Dockerfile](../../deploy/self-host/Dockerfile) 基于上游后端镜像安装 `backend/requirements-fork.txt`、生成 `self_hosted` 表，以 `uvicorn fork.main:app` 启动 → [backend/fork/main.py](../../backend/fork/main.py) 先执行 `bootstrap(Role.API)`，再 `from main import app` → [backend/fork/bootstrap.py](../../backend/fork/bootstrap.py) 验证 profile/依赖和环境，装入 Firestore 兼容层，应用角色相关的 patch → 上游路由在这些依赖下运行。API 导入后，fork 还会安装认证、能力、语音等路由/中间件适配器。[backend/fork/worker.py](../../backend/fork/worker.py) 和 [backend/fork/memory_maintenance_worker.py](../../backend/fork/memory_maintenance_worker.py) 是各自的工作进程入口；不是用 API 入口冒充后台任务。

| 原上游依赖 | 实际接管代码 | 边界与条件 |
|---|---|---|
| Firestore → PostgreSQL | [fork/firestore_pg/compat.py](../../backend/fork/firestore_pg/compat.py) 的 `install()` 在上游数据库模块导入前将 `google.cloud.firestore` / `firestore_v1` 注册为指向 PostgreSQL client 的 SDK facade；[bootstrap.py](../../backend/fork/bootstrap.py) 调用并检查数据库 schema。 | 复用上游 `database._client` 和查询调用者；只接受 profile 声明 `firestore_pg`，不是修改每个数据库调用点。 |
| Firebase 认证 → Better Auth | [patches/auth.py](../../backend/fork/patches/auth.py) 改绑上游 token/HTTP/WS 用户身份依赖和删除账户身份查询；[utils/auth_shim.py](../../backend/utils/auth_shim.py) 校验 Better Auth JWT 并提供兼容的用户 ID。独立的 [auth-server/src/index.js](../../auth-server/src/index.js) 将 `/api/auth/*` 交给 Better Auth，[auth-server/src/auth.js](../../auth-server/src/auth.js) 用 PostgreSQL/JWT。 | 上游业务路由仍在；这里不是把 Firebase SDK 整体改写成 Better Auth。服务端需有效 JWKS/密钥及独立 Auth 服务。 |
| GCS → MinIO | [patches/storage.py](../../backend/fork/patches/storage.py) 将上游 `utils.other.storage._get_storage_client` 指向 [storage_minio.py](../../backend/fork/storage_minio.py)。 | 原有 bucket/helper 调用保留；仅 profile 的 `object_store=minio` 启用。 |
| Cloud Tasks → Redis | [patches/queue.py](../../backend/fork/patches/queue.py) 改绑同步、音频合并、账号删除和对话终结的生产者及任务验签入口；[fork/worker.py](../../backend/fork/worker.py) 消费 Redis 队列。 | 原上游任务处理器继续工作；旧的 `*_DISPATCH_MODE=cloud_tasks` 环境值不能单独证明生产者仍发往 Cloud Tasks。 |
| Pinecone → Qdrant | [patches/vector.py](../../backend/fork/patches/vector.py) 替换 `database.vector_db.index`，[patches/embedding.py](../../backend/fork/patches/embedding.py) 改绑 embeddings 句柄。 | 保留向量业务调用者；Qdrant 配置、索引 schema/维度不符时拒绝启动或操作。 |
| 外部模型与语音 | [patches/llm.py](../../backend/fork/patches/llm.py) 改绑模型配置、默认 client、工具调用入口；[patches/speech.py](../../backend/fork/patches/speech.py) 改绑 STT selector/provider。TTS 与若干语音端点通过 [speech_transport.py](../../backend/fork/speech_transport.py) 的 `install(app)` 改绑路由 callable，**不是**一个通用 TTS 工厂 patch。 | [bootstrap.py](../../backend/fork/bootstrap.py) 按 profile 校验模型与 provider；[patches/capabilities.py](../../backend/fork/patches/capabilities.py) 阻止禁用能力的上游默认 provider；不能据此推断任意语音或推送服务均已交付。 |

[backend/fork/registry.py](../../backend/fork/registry.py) 的 `Patch` / `build_registry` / `apply` 先解析、检查**所有**适用的上游模块属性，然后才逐个 `setattr`；目标缺失即失败，而不是静默降级。[backend/fork/patches/__init__.py](../../backend/fork/patches/__init__.py) 是真实补丁集合，含身份、对象存储、队列、向量、LLM、语音等。Firestore SDK facade 是 bootstrap 前置步骤，**不属于**该 patch 注册表。自托管配置与各进程命令见 [compose.production.yml](../../deploy/self-host/compose.production.yml)；启动/边界的可执行证明见 [test_startup_contract.py](../../backend/fork/tests/test_startup_contract.py)、[test_patch_registry.py](../../backend/fork/tests/test_patch_registry.py)、[test_real_seams.py](../../backend/fork/tests/test_real_seams.py)。

## Web：只在构建副本覆盖上游页面/服务适配

[deploy/web/build.ts](../../deploy/web/build.ts) 的 `buildWeb` 选择目标及 profile → [deploy/web/source-stage.ts](../../deploy/web/source-stage.ts) 把上游 `web/app` 拷贝到独立输出目录 → 按 [web/app/fork/overlays.json](../../web/app/fork/overlays.json) 的精确路径替换 Firebase、AuthProvider、登录页、公开分享代理等文件，并增添分享页 → 在副本上执行其他 MCP/实时协议转换 → 编译同一套 Moonshine 应用。`self_hosted` 生成 Bun 启动产物，`cloudflare` 生成 Worker 与静态资源；这不是在仓库上游 `web/app/src/...` 直接改源码，也不是把应用重写为另一套 Web 框架。staging 校验路径、碰撞和原文件形状，拒绝覆盖源目录；[deploy/web/build.test.ts](../../deploy/web/build.test.ts) 验证这些边界。构建器目前返回 `release_ready: false`：构建产物不等于双目标已完成发布验收。

## Cloudflare：独立 Worker，而非复用自托管补丁

[deploy/cloudflare/workers/edge/index.ts](../../deploy/cloudflare/workers/edge/index.ts) 根据路由/绑定分发至独立的 Workers；[deploy/cloudflare/python/api-core/src/entry.py](../../deploy/cloudflare/python/api-core/src/entry.py) 是 Python API Core 入口，[deploy/cloudflare/python/core_entrypoint.py](../../deploy/cloudflare/python/core_entrypoint.py) 交接 Worker 请求；[deploy/cloudflare/workers/auth/index.ts](../../deploy/cloudflare/workers/auth/index.ts) 使用 D1 上的 Better Auth，与自托管 Express/PostgreSQL 是两套部署实现，共用 [auth/shared/jwt-policy.mjs](../../auth/shared/jwt-policy.mjs) 的令牌策略。不是 `fork.main` 加 `firestore_pg` 在 Cloudflare 上跑。

路由所有权/部署状态以 [routes.yaml](../../deploy/cloudflare/manifests/routes.yaml) 和 [backend-routes.json](../../deploy/cloudflare/manifests/backend-routes.json) 为准；后者明确区分 `staging-owned` 与 `blocked`。Edge 对尚未迁移的路由可由显式 `ORIGIN_BACKEND_URL` 转发，未配置时返回 404，不能把这种转发算作 Worker 原生实现；也不能把 staging-owned 算作生产合格。

## 客户端与设备的接缝

| 平台 | 隔离构建入口与实际接管点 | 上游保护/证明 |
|---|---|---|
| Flutter（当前仅 Android 本地调试验证） | [app/fork/prepare.py](../../app/fork/prepare.py) 的 `stage()` 按 profile 和私有品牌把受 Git 跟踪的 `app/` 复制到仓库外；在副本中复制 [fork/identity/](../../app/fork/identity/) 并替换认证、env、Crashlytics、通知服务的指名文件；[dart_overlay.dart](../../app/fork/dart_overlay.dart) 只修改审定的 Dart 成员，`AuthService` 的 token gateway 指向 `NativeIdentity.owner`，移除 Firebase 初始化和 FCM 能力。 | [source-owners.json](../../app/fork/source-owners.json) 的源 hash 漂移即拒绝 staging。唯一源码 T1 例外 [app/lib/flavors.dart](../../app/lib/flavors.dart) 将品牌标题接入 fork；其他上游文件不提交改动。 |
| macOS | [desktop/macos/fork/prepare.py](../../desktop/macos/fork/prepare.py) 的 `stage()` 将 `Desktop/` 拷贝到独立目录，装入 [ForkNative 身份代码](../../desktop/macos/fork/Sources/NativeIdentity/)，以 [swift_overlay.py](../../desktop/macos/fork/swift_overlay.py) 精确改写 `AuthService.swift`、`OmiApp.swift` 的 Firebase 启动/身份方法，并覆盖 `SignInView.swift`、`DesktopBackendEnvironment.swift` 的 staged 版本。 | [source-owners.json](../../desktop/macos/fork/source-owners.json) 校验上游 owner；输出必须在仓库外。构建前客户端 profile/品牌决定 API、Auth 和 bundle 身份，非全局替换已安装 Omi 应用。 |
| Windows/Linux Electron | [desktop/windows/fork/prepare.py](../../desktop/windows/fork/prepare.py) 将源码复制到仓库外，生成 `fork/native/profile.generated.ts`，运行 [source-stage.mjs](../../desktop/windows/fork/source-stage.mjs)；该脚本在副本中将 main 注册改成 [fork/native/runtime](../../desktop/windows/fork/native/runtime.ts) 的 `registerIdentity` / `currentBackendSession`，移除旧的 renderer 凭据桥，并把 Firebase/auth 模块引用指向 fork 版本。 | [source-owners.json](../../desktop/windows/fork/source-owners.json) 和精确调用/属性匹配防止上游漂移时静默误改；没有在原始 `src/` 增加 Vite 全局别名。 |
| CV1 固件 | [omi/firmware/fork/stage.py](../../omi/firmware/fork/stage.py) 从上游 `omi/firmware/omi` 拷贝一份，在副本的 `omi.conf` 替换 BLE/DIS 字符串、`src/lib/core/nfc.c` 替换配对 URL；生成发布策略/构建摘要。 | 要求唯一匹配、合法 URL 和独立新输出；脚本**仅 staging**，不读取签名密钥，也不负责发布。 |

以上是**代码接管机制**，不是发布资格声明：Flutter 的 [验证记录](../../app/fork/VERIFICATION.md) 只覆盖本地 Android debug，iOS/发布/OAuth/推送等未在此证明；macOS 的 [本地构建脚本](../../desktop/macos/fork/build.py) 标注 `release_ready: false`，Windows 的 [构建记录](../../desktop/windows/fork/prepare.py) 标注 `release_qualified: false`，编译不等于在 Windows/Linux UI 或系统凭据存储上验收。相应的 staging 防漂移证据见 [app/fork/test_stage.py](../../app/fork/test_stage.py)、[desktop/macos/fork/test_stage.py](../../desktop/macos/fork/test_stage.py)、[desktop/windows/fork/tests/test_prepare.py](../../desktop/windows/fork/tests/test_prepare.py)。

## 排查顺序与边界

1. 先查选中的 `deploy/profiles/<target>.yaml` 和渲染后的目标/阶段/品牌，再查对应部署的真正入口命令或 Worker binding；不要根据目录名猜当前进程执行了什么。
2. 自托管查 `fork.main` → `bootstrap` → `compat.install` → `patches/__init__.py` → 具体 patch → 上游消费方；若是端点重绑定，查 `fork.main` 安装的 transport。Cloudflare 改查 Edge/对应 Worker 及路由清单。
3. Web/客户端先区分**原始上游文件**与**staging 输出**。只有 [上游修改白名单](upstream-touch-allowlist.yaml) 中的源文件能直接产生 fork diff；构建产物上的替换不是上游提交。
