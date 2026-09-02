# 03 · 两个部署目标的目录契约：`deploy/self-host/` 与 `deploy/cloudflare/`

> 目标：两个目录各自完整、互不 import，但对客户端呈现**同一契约**（OpenAPI + WebSocket 协议 + 身份契约 v1），并由同一套一致性套件验证。任何一方的运行时细节不得泄漏到 `app/`、`desktop/`、`web/`（客户端只认 profile）。

## 1. 边界与不变式

| 不变式 | 自托管 | Cloudflare | 守卫 |
|---|---|---|---|
| 目录自足 | `deploy/self-host/` 只依赖 `backend/`（上游单体 + shim）与 `auth-server/` 镜像 | `deploy/cloudflare/` 不 import `backend/main.py`（现状：`python/api-core` 174 个文件零 import） | `fork-upstream-touch` + 静态 import 检查（CF 已有 `ARCHITECTURE.md:6-8` 规则） |
| 契约同一 | 服务 `docs/api-reference/*openapi*.json` 全集 | 服务 `manifests/routes.yaml` 中 `target_runtime ≠ legacy|blocked` 的子集，其余按 §4 处理 | `deploy/cloudflare/scripts/route_inventory.py --check`（从 CF 分支的 `export_openapi.py` 改动迁出，不改上游脚本）+ `contracts/` 套件对两者跑 |
| 身份契约 v1 | `auth-server/`（Express + Better Auth + PG） | `workers/auth`（Hono + Better Auth + D1） | `contracts/auth/` 套件（`02-deployment-profile.md` §6） |
| 数据面不共享 | firestore_pg（PostgreSQL JSONB）+ Qdrant + MinIO + Redis | D1 + Vectorize + R2 + Queues + DO | 迁移工具是显式、单向、账户级的（`INV-DATA-1`/`INV-CUTOVER-1`），没有双写 |
| 无上游供应商锁定 | `SELF_HOST_EGRESS_ALLOWLIST` 拒绝 `*.omi.me`、`api.openai.com` 等（`deploymentProfile.ts:31-54` 同款） | 出站限于 Cloudflare 服务 + 运营方端点 | `zero-vendor-acceptance.sh`（自托管已有）；CF 侧新增等价脚本 |

## 2. `deploy/self-host/` 契约

**现状（shim 分支，24 文件）**：`compose.production.yml` 11 个服务（postgres 16.4、redis 7.4、minio、qdrant 1.15、typesense 29、searxng、auth-migrate、firestore-pg-migrate、auth-server、backend、queue-worker），全部端口绑定 `${SELF_HOST_BIND_ADDRESS}`；`.env.production.example` 分组齐全；`operations.sh`（self-check/start/status/runtime-evidence/metrics/backup/verify-backup/restore/rollback-plan）；16 个验收脚本；`nginx.cutover-acceptance.conf` 把 `/internal/` 404。

**合入 main 时的调整**：

| 项 | 改动 |
|---|---|
| 镜像 | `BACKEND_IMAGE` 由 `deploy/self-host/Dockerfile` 构建：`FROM` 上游 `backend/Dockerfile` 产物层 + `pip install -r backend/requirements-fork.txt`（不改上游 requirements/锁文件）；入口 `uvicorn fork.main:app`（`backend/fork/main.py` import 上游 `main.app` 后应用补丁注册表），上游后端文件零改动 |
| 环境 | `.env.production.example` 改为由 `scripts/profiles/render.py --target self_hosted --stage production --brand <id>` 生成 `.env.production`（品牌域名、`OMI_DEPLOYMENT_PROFILE=self_hosted`、`CAPABILITIES_JSON`）；示例文件保留为文档 |
| Web | 新增 `web` 服务：上游 `web/app/Dockerfile`（Next.js standalone）+ profile 注入（`window.__DEPLOYMENT_PROFILE__`）；反向代理路径 `/api/auth/*` → auth-server、`/api/proxy/*` → backend |
| Web 实时 | 沿用上游 `/v4/web/listen` 首帧 token 协议，无新增路由；Web 端用 cookie 会话经 `/api/auth/token` 换 JWT 后按上游方式发首帧 |
| CI 版 compose | `compose.ci.yml`：去掉 searxng/typesense（或 stub），STT/TTS/LLM 用 `PROVIDER_MODE=offline` stub；`ci/contract.sh` 起栈 → 跑 `contracts/` + OpenAPI `--check` + `auth-flow-smoke.py` + 一条对话闭环 |
| 发布 | 标签 `<brand>/selfhost/v*` → `fork-deploy-selfhost.yml`：构建两镜像 → 推 `${FORK_REGISTRY}` → `operations.sh deploy`（新增子命令：拉镜像、`compose up -d`、`auth-migrate`/`firestore-pg-migrate` 一次性任务、`cutover-live-smoke.py`） |
| 备份 | `volume-snapshot.py` 已有；补 `pg_dump` 逻辑备份与 MinIO 桶同步到运营方对象存储的 cron 示例 |

## 3. `deploy/cloudflare/` 契约

**现状（CF 分支，616 文件）**：TS Workers `edge`（639 条 Hono 字面路由）、`auth`、`jobs`、`rate-limit`（DO）、`realtime`（DO）；Python Workers `api-core`（434 路由，80 模块）、`api-ai`（30 路由）；`migrations/{auth(10),app(155)}`；`manifests/`（`routes.yaml` 628 条含 owner/target_runtime/auth_authority/rollback、`backend-routes.json` 577 条含 `migration_state`、`redis-primitives.yaml` 每个 Redis 键族→D1/KV/DO/Queue/Workflow/R2 映射、`resources.yaml`、`r2-namespaces.yaml`、`vector-namespaces.yaml`）；`scripts/deploy.mjs` 完整资格流程；104 个 TS 测试 + 70 个 Python 测试；独立生产已上线（workers.dev 域）。

**合入 main 时的调整**：

| 项 | 改动 |
|---|---|
| **契约权威** | Cloudflare 单向对齐**上游 + 自托管**：所有客户端可见的路径、头、错误码、WS 首帧以上游 OpenAPI/协议为准，自托管为参考实现；差异只允许存在于 `deploy/cloudflare/` 内部（Worker 间断言头、DO、D1、限流实现）。CF 分支里任何"客户端为了 CF 而改"的东西（web-ticket、bootstrap 头、`/v1/capabilities`、409 账户围栏、WAL 批量硬编码）都按此原则撤回或降为内部实现 |
| 身份 | Auth Worker `basePath` → `/api/auth`；JWT TTL/`iss`/`aud` 读 profile vars；Edge 对 bearer JWT 改 JWKS 校验（已声明的 `BETTER_AUTH_JWKS_URL/ISSUER/AUDIENCE` 接上），`/internal/verify` 仅供 cookie 会话；Firebase scrypt 导入逻辑抽到 `auth/shared/`（见 §5） |
| 品牌 | 所有 `omi-cf-*` 资源名、`workers.dev` 子域、`x-omi-*` 内部头名由 `apply.py` 渲染 `wrangler.<stage>.jsonc`（`brand.id` 前缀） |
| 未迁移路由 | `LEGACY_BACKEND_URL` 语义改为 **`ORIGIN_BACKEND_URL`**：可指向同一品牌的自托管后端（混合部署，§4）；未配置时返回与上游同形的 404（不新增 `/v1/capabilities` 之类上游没有的端点）；客户端从**静态 profile** 的能力表得知该目标不提供哪些路由族并隐藏入口 |
| 账户围栏 | `cloudflareProductTrafficDenial`（409）读 profile `account_activation_fence`，新品牌默认关闭；它是 `omi_cloud→cloudflare` 迁移工具的内部状态，不得进入客户端契约 |
| 请求限制 | `sync_upload_batch_limit=2`、`max_request_bytes=100MB` 进 profile（客户端读表） |
| Web | vinext 构建保留（`web/app/vite.config.ts`、`wrangler.jsonc`、`next.config.js` 的 `VINEXT_BUILD` 别名为**加法**，不替换上游 Next 配置）；`NEXT_PUBLIC_AUTH_MODE` 退役为 profile；`/v4/web/listen` 改回上游首帧 token 协议，删除 web-ticket 与 bootstrap 头 |
| CI | `ci/contract.sh`：`wrangler dev`（miniflare + 本地 D1 迁移）起 auth/rate-limit/api-core/api-ai/edge → 同一 `contracts/` 套件；`npm run validate:manifest`、`verify:migrations`、`validate:backend-routes` 进 `checks-manifest.fork.yaml`（现有 `pretest` 保持） |
| 发布 | 标签 `<brand>/cloudflare/v*` → `fork-deploy-cloudflare.yml` 调 `scripts/deploy.mjs`（顺序 rate-limit → auth → jobs → realtime → api-* → edge → web；D1 迁移 apply + `verify:migrations`；`smoke:production`）；`CLOUDFLARE_PRODUCTION_CONFIRM` 短语由工作流 `environment` 审批门代替 |
| 工具链钉死 | wrangler `4.127.0`、`uvx uv==0.12.3 run pywrangler`、Python ≥3.13、Node 22 —— 写进 `deploy/cloudflare/.tool-versions` 与工作流 |

## 4. 混合部署（可选，但只有单仓库才可能）

```
客户端 ──► Cloudflare Edge Worker（鉴权、限流、静态、实时 DO、已迁移的 api-core/api-ai 路由）
               │  routes.yaml 中 target_runtime=legacy 的路由
               ▼
          ORIGIN_BACKEND_URL = 品牌自托管 backend（`deploy/self-host` 的同一镜像）
```

- 数据面仍**不共享**：混合部署下 Edge 承担的路由必须是无状态或以 DO/KV 为状态的（限流、票据、实时中继、静态），有状态业务路由要么全在 Workers（D1），要么全在自托管（PG）。`routes.yaml` 的 `owner` 字段就是这条线；`validate-manifests.mjs` 增加规则：`target_runtime=legacy` 的路由不得与 `python-worker` 路由共享同一 D1 表族（用 `redis-primitives.yaml` 同款映射表声明）。
- 混合部署是 profile 的第四个取值吗？**不是**：客户端仍是 `cloudflare` profile（只认 Edge origin）；混合是 Cloudflare 目录内部的部署选项（`ORIGIN_BACKEND_URL` 是否配置）。

## 5. 共享的服务端资产（放 fork 路径，两目录 import）

| 资产 | 路径 | 用途 |
|---|---|---|
| Better Auth 共享配置 | `auth/shared/`（TS 包）：JWT 插件参数（ES256、TTL、轮换）、claims 形状（`sub`+`uid`）、Firebase scrypt 校验与首登重哈希、`omi-capabilities` 端点形状 | `auth-server/`（Express+PG）与 `deploy/cloudflare/workers/auth`（Hono+D1）各写一个 adapter（存储 + HTTP 框架），逻辑只有一份 |
| Web 同源代理 | `web/app/src/lib/auth-proxy.ts`（CF 已有）改为运行时无关（`fetch` 抽象）：Next 服务端路由与 vinext Worker 共用 | 会话令牌剥离、`Set-Cookie` 转发、`redirect: manual` |
| Web 实时鉴权 | 上游 `/v4/web/listen` 首帧 token 协议（`contracts/realtime/web-listen.md` 只记录上游现状与夹具）；CF Edge 对首帧 JWT 做 JWKS 校验 | 两端同一首帧格式，来源是上游 |
| 限流策略表 | 上游 `backend/utils/rate_limit_config.py`（只读）；CF 补充策略放 `deploy/cloudflare/manifests/rate-limits.yaml`，fork 测试断言其 ⊇ 上游表 | 自托管直接用上游表；CF 镜像 |
| 路由清单 | `deploy/cloudflare/manifests/backend-routes.json` 由 `deploy/cloudflare/scripts/route_inventory.py` 生成（import 上游 FastAPI app 与 `export_openapi.py` 的 hermetic bootstrap 函数，不修改上游脚本） | 新上游路由出现即失败，强制分类（CF 分支 2026-08-29 404 事故的守卫） |
| 契约套件 | `contracts/`（上游 parity 夹具）+ 新增 `contracts/auth/`、`contracts/realtime/`、`contracts/api-smoke/`（登录→录音上传→对话→记忆→导出） | `fork-contract-selfhost.yml`、`fork-contract-cloudflare.yml` 共用 |

## 6. 决策 D3：Web 运行时（已定：保留上游 Next.js，不引入 Bun）

- 事实：shim 分支把 `web/app` 从 Next.js 迁到 **Moonshine on Bun**（删除 `next.config.js`、`next-env.d.ts`、`package-lock.json`、`vitest.config.ts` 与 `src/app/**` 路由，新增 138 个文件）；CF 分支保留 Next.js 源并用 **vinext** 构建到 Workers（`vite.config.ts` + `wrangler.jsonc` + `next.config.js` 的条件别名，纯加法）。二者互斥，且 Moonshine 迁移与上游 `web/app` 的每次改动都会冲突（上游 web/app 是活跃目录）。
- **推荐**：保留上游 Next.js 源为唯一 Web 源码；自托管目标用 `next build` standalone 容器（上游 `web/app/Dockerfile` 已有）；Cloudflare 目标用 vinext；Moonshine 迁移打 tag `archive/web-moonshine-2026-09` 后不合入。Web 运行时**不引入 Bun**：自托管用上游 `web/app/Dockerfile` 的 Node standalone 镜像原样运行，与上游同步。
- 影响：`01-branch-consolidation.md` 的 M2 步骤按此执行；20 个互冲突文件中 `web/app` 的 13 个全部以 CF 分支版本为基础重做。

## 7. 未移植清单（Cloudflare 目标的已知缺口，进 `/v1/capabilities`）

来自 `python/api-core/ARCHITECTURE.md`：workstream 搜索/索引刷新与候选自动化；应用评论的推送投递；意图物化与再提醒调度；批量 speech-profile 指派；对话重处理与合并。处置：① 混合部署时由 `ORIGIN_BACKEND_URL` 承接；② 纯 Cloudflare 部署时客户端按能力表隐藏入口；③ 每季度复审清单，移植一项就从能力表删一项。
