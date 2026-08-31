# Legacy 路由迁移审计

截至 2026-08-31，`backend-routes.json` 中还有 83 条 `legacy-owned` 路由。这个清单不是把路由简单改成 Cloudflare 代理：只有当数据 authority、认证边界、异步重试和外部 provider 语义都能在 Workers 上闭合时，才允许把 owner 改成 `staging-owned`。

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
| Desktop release mutation/manifest | `/v2/desktop/releases*`、beta admission/promotion | beta admission/promotion CAS、release pipeline 回填和生产 Firestore→D1 回放 | 完整 immutable manifest 已进入 D1；GET/POST manifest 在 staging 由 API Core 提供，下一步迁移发布流水线回填并复验晋级族 |
| Metrics / wrapped / analytics | `/metrics`、`/v1/wrapped/*`、部分分析端点 | Prometheus/历史分析数据不在当前 D1 authority | 定义聚合与保留策略；不能以空响应冒充迁移完成 |
| Hume callback / provider webhooks | `/v1/agents/hume/callback` | 外部 webhook schema、幂等、长处理和重试 | 先落 Queue receipt 与 provider signature contract，再切 webhook owner |

## 当前可执行顺序

1. 让 release pipeline 回填已迁移的 D1 immutable manifest，并完成 Firestore→D1 回放后，再迁移 beta admission/promotion 与 channel mutation。
2. 设计并落地 memory review-conflict D1 表及 producer，随后迁移 review queue 的三个端点。
3. 建立 conversation finalization 的 Jobs/D1 lease projection，成组迁移 status、finalize、reprocess 和 merge。
4. 完成 candidate/recommendation authority 后，再处理 staged-tasks 与 What Matters Now；在此之前保持现有 404/legacy 边界。

每一组迁移都必须同时更新 route manifest、Edge owner、回归测试、删除/残留清理和 staging live evidence；不能仅添加同路径 alias 来降低 legacy 计数。
