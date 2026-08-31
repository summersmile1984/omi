# Legacy 路由迁移审计

截至 2026-08-31，`backend-routes.json` 中还有 66 条 `legacy-owned` 路由。这个清单不是把路由简单改成 Cloudflare 代理：只有当数据 authority、认证边界、异步重试和外部 provider 语义都能在 Workers 上闭合时，才允许把 owner 改成 `staging-owned`。

## 分组与迁移前置条件

| 路由分组 | 代表路径 | 主要缺口 | 下一步 |
| --- | --- | --- | --- |
| Auth / OAuth / social | `/v1/auth/*`、`/v1/oauth/*`、`/v1/apps/mcp/callback`、Twitter ownership | Firebase provider identity、Google/Apple callback、OAuth app consent 仍由 legacy 维护；Better Auth 目前只承接 MCP OAuth 和会话 | 先完成身份连续性与 provider link/import，再迁移 callback/token；不能用 Better Auth session 别名替代 Firebase exchange |
| Phone / external integrations | `/v1/phone/*`、电话 webhook | Twilio、Redis 状态、号码验证和电话 webhook | 定义 DO/Queue 状态机、签名校验和 provider secret 生命周期后再迁移 |
| Conversation lifecycle | `/v1/conversations/{id}/finalization`、`finalize`、`reprocess`、`merge` | merge 仍依赖 Firestore、Cloud Tasks lease 和 legacy fan-out | finalize/status/reprocess 已接入共享 D1 job projection 与 Jobs consumer；下一步迁移 merge 并补齐 reprocess staging live evidence |
| Import / files / sync / audio | `/v1/import/*`、`/v1/files`、`/v2/files`、sync/audio jobs | GCS/本机临时文件、multipart、长任务和 R2 residual contract | 先完成 R2 multipart/presigned 与 Queue replay；单独迁移只读状态不能闭合上传语义 |
| Task intelligence / staged tasks | `/v1/staged-tasks*`、`/v1/task-intelligence/*`、`/v1/what-matters-now*` | candidate/recommendation store、generation fence、LLM judgment、device snapshot | candidate D1 projection 和 generation contract 完成前保持 fail-closed legacy owner |
| Persona / apps | `/v1/personas` mutation、`/v1/apps/*` MCP mutation、Twitter ownership | multipart 图片、R2、LLM prompt、Twitter provider identity 与 public app cache | 默认 Persona 和只读 profile 已迁移；通用 Persona mutation/Twitter ownership 需完整 D1/R2/provider contract |
| Memory admin / Archive | `/memory/admin/*`、`/memory/archive/search` | Archive capability | `/memory/search`、`/memory/vector/search` 与 D1 review queue 已迁移；archive/admin 仍需补齐 capability |
| Desktop release mutation/manifest | release pipeline 回填和生产 Firestore→D1 回放 | 发布流水线凭据、历史 manifest 回放和生产切换审计 | immutable manifest、macOS Stable/Beta channel pointer、Beta admission/promotion/breakglass、legacy release bridge（`/updates/releases` POST/PATCH）和 cache-invalidation contract 已进入 D1；所有相关 endpoint 在 staging 由 API Core 提供，下一步回填发布流水线并复验 Beta 晋级族 |
| Metrics / wrapped / analytics | `/metrics`、`/v1/wrapped/*`、部分分析端点 | Prometheus/历史分析数据不在当前 D1 authority | 定义聚合与保留策略；不能以空响应冒充迁移完成 |
| Hume callback / provider webhooks | `/v1/agents/hume/callback` | 外部 webhook schema、幂等、长处理和重试 | 先落 Queue receipt 与 provider signature contract，再切 webhook owner |

## 当前可执行顺序

1. 让 release pipeline 使用 `.github/scripts/backfill-desktop-release-manifest.py` 回填已迁移的 D1 immutable manifest；Stable/Beta promotion、legacy release bridge 和 `clear-cache` 已由 API Core/D1 承接，完成 Firestore→D1 回放后再注入发布流水线凭据并复验 Beta 晋级。该工具只读 legacy manifest、验证 v1 digest，再向 Cloudflare Edge 的 `/v2/desktop/releases` 做幂等 POST，不改变 channel pointer。
2. 对已接入的 conversation finalization/reprocess Jobs/D1 lease projection 做 staging live 验证，再迁移 merge。
3. 完成 candidate/recommendation authority 后，再处理 staged-tasks 与 What Matters Now；在此之前保持现有 404/legacy 边界。

Review queue 已完成第一阶段闭环：D1 canonical memory 写入会产生结构化冲突记录；三个 review-queue 端点由 API Core/Edge 承载；每次读取校验来源 `updated_at` revision 与 SHA-256 content hash，来源变化自动 tombstone；accept/reject/correct/timeout 解析具备 D1 原子写入和幂等状态。该阶段的 producer 覆盖 canonical `/v3/memories` 与 `/v3/memories/batch` 写入，手工已确认的 MCP/developer memory 不会重新进入队列。

本轮新增 `GET/POST /v1/agent/*`：API Core 只返回并执行已由 D1/Workers 实现的对话、记忆和任务工具，动态第三方 App 与未迁移的 provider 工具继续 fail-closed，避免把未实现的能力暴露给模型。每一组迁移都必须同时更新 route manifest、Edge owner、回归测试、删除/残留清理和 staging live evidence；不能仅添加同路径 alias 来降低 legacy 计数。

## Staging evidence（2026-08-31）

- 最近一次完整 staging 发布（2026-08-31）的 Worker 版本为：Auth `70d200b0-5782-45f5-b580-47972f5eaf6c`、Rate Limit `9e07450b-71b6-40cd-bc20-6dce852bd354`、API Core `50fb07d7-f464-43aa-a6ea-9560360c4280`、API AI `0cb00547-0ab3-4480-a7ef-3735fecd0a6e`、Realtime `50476c78-b356-4e7e-86b4-d8e3a569a192`、Jobs `f7e2f73b-4409-4def-96ff-18d3bb4366a9`、Edge `a991ab93-8013-40b9-8172-4a2049ffe5e9`、Web `055963c1-bfd3-4d6d-bf2c-f02114df2f3f`。
- `POST /updates/releases` 和 `PATCH /updates/releases/promote` 通过 Edge 实测返回 `401 Invalid or missing X-Release-Secret header`（不是 404），证明请求已进入 API Core 的新 owner；staging 当前尚未注入 `RELEASE_SECRET`，因此没有执行真实写入探针。
- `/health` 通过 Edge 实测返回 `200`；`POST /v2/desktop/beta/candidates/reserve`、`POST /v2/desktop/beta/promote-candidate`、`PUT /v2/desktop/beta/admission`、`POST /v2/desktop/beta/breakglass` 在缺少凭据时分别返回 `401`、`401`、`403`、`403`（均非 `404`）。staging 尚未注入 `BETA_PROMOTION_TOKEN`/`GITHUB_TOKEN`，因此没有伪造 Beta 正向 promotion 或 GitHub 证据结果。
- `npm run smoke:staging` 通过：Edge/v1 health、Apple association、OpenAI Apps challenge、公告、趋势、app reviews、支付边界均符合预期；authenticated checks 因未提供 smoke bearer token 而跳过。Calendar Google events 未认证探针返回 `401`（不是 `404`）。
- `/memory/vector/search?query=coffee&limit=3` 通过专用合成账号实测：未认证返回 `401`，Better Auth 认证后空 D1/Vectorize 结果返回 `200` 且 `search_status=ok`、`items=[]`；账号随后通过 `DELETE /v1/users/delete-account` 清理。此次发布版本为 API Core `50fb07d7-f464-43aa-a6ea-9560360c4280`、Edge `a991ab93-8013-40b9-8172-4a2049ffe5e9`。
- Memory review queue staging 数据面已实测：远端应用 `0093_memory_review_queue.sql` 后，API Core `e6e64b52-7215-44c7-9c91-0b5a9d91100a`、Edge `a39cd3d1-3524-472e-8935-8eeabc37e0a2`、Jobs `3f9efdd3-8695-4f16-8ca8-fdeb20df7342` 已发布；临时 Better Auth 账号通过 Edge 连续创建结构化冲突、读取到 `pending` queue、`accept` 得到 `accepted` 与确定性 D1 commit，并提交删号清理。未认证的三个 queue 路由均返回 `401` 而非 `404`。
- Conversation finalization 的 admission/status、D1 revision/job schema、Jobs lease/retry/reconcile 和 API Core Workers AI processor 已实现并有 Python/TypeScript 回归测试；已完成 staging migration 与正向队列验证，但不宣称生产等价。
- Conversation finalization staging live evidence（2026-08-31）：远端应用 `0094_conversation_finalization_jobs.sql` 后，API Core `cb1a8271-22f2-4211-9ba4-94d7463c4745`、Jobs `18df02b0-7ad1-4010-a063-46144d584899`、Edge `99eb710f-03c3-41bf-8f29-763630690393` 已发布。隔离 Better Auth 账号经 Edge 完成 `POST /v1/conversations/{id}/finalize`（200），status 实际经历 `queued → running → completed`（attempt 1），会话读回 `status=completed`；随后公开删号完成 Queue 清理，`cf_conversations` 与 `cf_conversation_finalization_jobs` 均为 0，仅保留预期 tombstone。
- Conversation reprocess 已完成 D1/Queue 闭环：共享 `cf_conversation_finalization_jobs` 新增 `operation/language_code/app_id`，Jobs 重试/租约恢复时保留参数；API Core 重跑 Workers AI enrichment、按来源替换旧 action/memory 派生行与向量删除/重建 outbox，并支持同一 processing job 幂等返回。staging 正向证据见下方。
- Conversation reprocess staging live evidence（2026-08-31）：`0095_conversation_reprocess_jobs.sql` 已远端应用；API Core `23cc4631-1942-40eb-b67d-e8c3186d0786`、Jobs `db0f4aa5-cff3-4be8-acd3-6e817f37ab91`、Edge `ebec0033-35af-4c27-8c08-9cee3078fe2c` 已发布。隔离 Better Auth 账号经 Edge 创建会话（200），提交 `POST /v1/conversations/{id}/reprocess?language_code=en&app_id=calendar-app`（200），status 实际经历 `running → completed`（attempt 1）；会话读回 `completed/discarded=false`，action item 派生数据可读，`external_data` 无内部 claim marker。随后 `DELETE /v1/users/delete-account`（200）完成，tombstone 存在且 conversation/action/memory/reprocess job/vector outbox/vector state 残留均为 0。
- Agent tool directory staging live evidence（2026-08-31）：API Core `70bd49eb-f0db-4338-a166-0612af37eda7`、Edge `79ba9a42-e9fc-4819-b5ac-778487a5ee8b` 已发布；未认证 `GET /v1/agent/tools` 与 `POST /v1/agent/execute-tool` 均返回 `401`，Better Auth 认证后目录返回 `200` 与 7 个 Cloudflare-native 工具定义，执行 `get_memories_tool` 返回 D1 结果，未迁移的 `get_calendar_events_tool` 返回 `404`，响应不含 `config`，临时账号已提交删号。

## 本轮 authority 核对（2026-08-31）

- `POST /v1/import/limitless` 仍不能切到 Worker：legacy 会接收 multipart ZIP、落本机临时目录并启动后台解析；当前 D1 只有 `v3/memory-imports/batch` 的 artifact receipt，没有 ZIP 解包、导入 job、Queue 重试和同等的会话创建 authority。`retired_compat_routes.py` 中的 Limitless 删除接口是有意的零副作用兼容响应，不代表上传已迁移。
- `POST/PATCH /v1/personas*` 仍不能切到 Worker：创建/更新同时依赖图片上传、作者资料、用户名唯一化、Workers/legacy LLM prompt、以及公开目录缓存失效；当前 `cf_app_catalog` 仅是投影，直接写入会绕过这些约束。
- `/v1/oauth/*` 与 `/v1/apps/mcp*` 仍不能由 Better Auth session 直接替代：legacy token 路径验证 Firebase ID token、应用启用/付费状态及 OAuth state/PKCE；D1 的 MCP OAuth 表只覆盖已迁移的 `/v1/mcp/*`，不是外部应用动态注册流程。
- `/v1/conversations/merge` 和 `/v2/sync-jobs/run` 仍缺少同一 canonical conversation、lease、提取 fan-out 与 Queue consumer；finalize/status/reprocess 已有共享 D1/Jobs 边界，但仍需 reprocess staging live 验证，merge 尚未迁移。
- Desktop Beta 四条 mutation 已成组迁移：reserve/admission、signed GitHub evidence、manifest/pointer CAS 和 breakglass audit 共享 D1 控制面；没有 `BETA_PROMOTION_TOKEN`/`GITHUB_TOKEN` 时 promotion 与 emergency rollout 会 fail-closed，不会产生半写入。发布流水线凭据和历史 Firestore→D1 回放仍是 staging 正向验证前置条件。
