# Legacy 路由迁移审计

截至 2026-08-31，`backend-routes.json` 中还有 75 条 `legacy-owned` 路由。这个清单不是把路由简单改成 Cloudflare 代理：只有当数据 authority、认证边界、异步重试和外部 provider 语义都能在 Workers 上闭合时，才允许把 owner 改成 `staging-owned`。

## 分组与迁移前置条件

| 路由分组 | 代表路径 | 主要缺口 | 下一步 |
| --- | --- | --- | --- |
| Auth / OAuth / social | `/v1/auth/*`、`/v1/oauth/*`、`/v1/apps/mcp/callback`、Twitter ownership | Firebase provider identity、Google/Apple callback、OAuth app consent 仍由 legacy 维护；Better Auth 目前只承接 MCP OAuth 和会话 | 先完成身份连续性与 provider link/import，再迁移 callback/token；不能用 Better Auth session 别名替代 Firebase exchange |
| Phone / external integrations | `/v1/phone/*`、电话 webhook | Twilio、Redis 状态、号码验证和电话 webhook | 定义 DO/Queue 状态机、签名校验和 provider secret 生命周期后再迁移 |
| Conversation lifecycle | `/v1/conversations/{id}/finalization`、`finalize`、`reprocess`、`merge` | Firestore canonical 状态、Cloud Tasks lease、memory extraction、merge/reprocess fan-out | 先建立 D1 finalization job projection 与 Jobs consumer，再成组迁移 finalize/status/reprocess/merge |
| Import / files / sync / audio | `/v1/import/*`、`/v1/files`、`/v2/files`、sync/audio jobs | GCS/本机临时文件、multipart、长任务和 R2 residual contract | 先完成 R2 multipart/presigned 与 Queue replay；单独迁移只读状态不能闭合上传语义 |
| Task intelligence / staged tasks | `/v1/staged-tasks*`、`/v1/task-intelligence/*`、`/v1/what-matters-now*` | candidate/recommendation store、generation fence、LLM judgment、device snapshot | candidate D1 projection 和 generation contract 完成前保持 fail-closed legacy owner |
| Persona / apps | `/v1/personas` mutation、`/v1/apps/*` MCP mutation、Twitter ownership | multipart 图片、R2、LLM prompt、Twitter provider identity 与 public app cache | 默认 Persona 和只读 profile 已迁移；通用 Persona mutation/Twitter ownership 需完整 D1/R2/provider contract |
| Memory admin / Archive / Vector / review | `/memory/admin/*`、`/memory/archive/search`、`/memory/vector/search`、`/v3/memories/review-queue*` | Archive capability、Vectorize hydrate、Firestore review-conflict authority | `/memory/search` 已迁移；其余路线分别补齐 capability、projection 和 review producer 后再切换 |
| Desktop release mutation/manifest | release pipeline 回填和生产 Firestore→D1 回放 | 发布流水线凭据、历史 manifest 回放和生产切换审计 | immutable manifest、macOS Stable/Beta channel pointer、Beta admission/promotion/breakglass、legacy release bridge（`/updates/releases` POST/PATCH）和 cache-invalidation contract 已进入 D1；所有相关 endpoint 在 staging 由 API Core 提供，下一步回填发布流水线并复验 Beta 晋级族 |
| Metrics / wrapped / analytics | `/metrics`、`/v1/wrapped/*`、部分分析端点 | Prometheus/历史分析数据不在当前 D1 authority | 定义聚合与保留策略；不能以空响应冒充迁移完成 |
| Hume callback / provider webhooks | `/v1/agents/hume/callback` | 外部 webhook schema、幂等、长处理和重试 | 先落 Queue receipt 与 provider signature contract，再切 webhook owner |

## 当前可执行顺序

1. 让 release pipeline 使用 `.github/scripts/backfill-desktop-release-manifest.py` 回填已迁移的 D1 immutable manifest；Stable/Beta promotion、legacy release bridge 和 `clear-cache` 已由 API Core/D1 承接，完成 Firestore→D1 回放后再注入发布流水线凭据并复验 Beta 晋级。该工具只读 legacy manifest、验证 v1 digest，再向 Cloudflare Edge 的 `/v2/desktop/releases` 做幂等 POST，不改变 channel pointer。
2. 设计并落地 memory review-conflict D1 表及 producer，随后迁移 review queue 的三个端点。
3. 建立 conversation finalization 的 Jobs/D1 lease projection，成组迁移 status、finalize、reprocess 和 merge。
4. 完成 candidate/recommendation authority 后，再处理 staged-tasks 与 What Matters Now；在此之前保持现有 404/legacy 边界。

每一组迁移都必须同时更新 route manifest、Edge owner、回归测试、删除/残留清理和 staging live evidence；不能仅添加同路径 alias 来降低 legacy 计数。

## Staging evidence（2026-08-31）

- API Core `omi-cf-api-core-staging` deployed version: `9a5a3f8a-52ce-4b09-a43b-cf85f85751c8`。
- Edge `omi-cf-edge-staging` deployed version: `18d392cb-497d-4a3b-aca2-71529b5c1153`。
- `POST /updates/releases` 和 `PATCH /updates/releases/promote` 通过 Edge 实测返回 `401 Invalid or missing X-Release-Secret header`（不是 404），证明请求已进入 API Core 的新 owner；staging 当前尚未注入 `RELEASE_SECRET`，因此没有执行真实写入探针。
- `/health` 通过 Edge 实测返回 `200`；`POST /v2/desktop/beta/candidates/reserve`、`POST /v2/desktop/beta/promote-candidate`、`PUT /v2/desktop/beta/admission`、`POST /v2/desktop/beta/breakglass` 在缺少凭据时分别返回 `401`、`401`、`403`、`403`（均非 `404`）。staging 尚未注入 `BETA_PROMOTION_TOKEN`/`GITHUB_TOKEN`，因此没有伪造 Beta 正向 promotion 或 GitHub 证据结果。

## 本轮 authority 核对（2026-08-31）

- `POST /v1/import/limitless` 仍不能切到 Worker：legacy 会接收 multipart ZIP、落本机临时目录并启动后台解析；当前 D1 只有 `v3/memory-imports/batch` 的 artifact receipt，没有 ZIP 解包、导入 job、Queue 重试和同等的会话创建 authority。`retired_compat_routes.py` 中的 Limitless 删除接口是有意的零副作用兼容响应，不代表上传已迁移。
- `POST/PATCH /v1/personas*` 仍不能切到 Worker：创建/更新同时依赖图片上传、作者资料、用户名唯一化、Workers/legacy LLM prompt、以及公开目录缓存失效；当前 `cf_app_catalog` 仅是投影，直接写入会绕过这些约束。
- `/v1/oauth/*` 与 `/v1/apps/mcp*` 仍不能由 Better Auth session 直接替代：legacy token 路径验证 Firebase ID token、应用启用/付费状态及 OAuth state/PKCE；D1 的 MCP OAuth 表只覆盖已迁移的 `/v1/mcp/*`，不是外部应用动态注册流程。
- `/v1/conversations/*/finalize*`、`/v1/conversations/merge` 和 `/v2/sync-jobs/run` 仍缺少同一 canonical conversation、lease、提取 fan-out 与 Queue consumer；现有 sync-local-files consumer 只覆盖其独立的 D1/R2 文件同步 job。
- Desktop Beta 四条 mutation 已成组迁移：reserve/admission、signed GitHub evidence、manifest/pointer CAS 和 breakglass audit 共享 D1 控制面；没有 `BETA_PROMOTION_TOKEN`/`GITHUB_TOKEN` 时 promotion 与 emergency rollout 会 fail-closed，不会产生半写入。发布流水线凭据和历史 Firestore→D1 回放仍是 staging 正向验证前置条件。
