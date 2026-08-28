# Cloudflare 适配架构与完成度审计

本目录保存 Cloudflare Workers 适配的架构视图，以及截至 2026-08-28 的 staging 完成度审计。结论先行：当前已完成的是一条可运行、可验证的 Cloudflare staging 切片，不是完整的生产迁移目标。生产身份、生产数据、全部路由和旧基础设施清理均仍需单独的发布门槛。

## 架构图

| 图 | 内容 |
| --- | --- |
| [01-overview-current-target.png](./01-overview-current-target.png) | 当前 staging、迁移窗口和稳定目标架构 |
| [02-request-auth-flow.png](./02-request-auth-flow.png) | HTTP、WebSocket、异步任务和认证边界 |
| [03-data-async-plane.png](./03-data-async-plane.png) | D1、R2、DO、Queues/DLQ、Workers AI、外部 API 与 Vectorize 的数据/异步平面 |
| [04-migration-roadmap.png](./04-migration-roadmap.png) | CF-00～CF-13 路线图及完成度 |
| [05-route-ownership.png](./05-route-ownership.png) | 当前已声明路由的 Worker owner 和迁移窗口兜底 |

## CF-00～CF-13 完成度

| 项目 | 状态 | 已有证据 | 尚未闭合 |
| --- | --- | --- | --- |
| CF-00 清单/scaffold | 已完成 | hermetic FastAPI 注册表确认 577 条唯一后端路由（573 HTTP、4 WebSocket，含显式 HEAD/OPTIONS）；197 条已匹配 Cloudflare staging owner，380 条显式保留 legacy owner；215 条 Cloudflare 路由、15 个隔离资源、34 个 Redis family、7 个 vector namespace、9 个 R2 namespace 均有机械校验 | 未迁路由的实现工作归入 CF-10，不再是清单未知项 |
| CF-01 Edge | staging 已验证 | header 剥离、防重放、CORS、owner 标记、health/smoke | 生产回滚与非幂等超时的 live 证据 |
| CF-02 Python Workers | staging 已验证 | api-core/api-ai、110+37 个 Python 测试、真实 Worker smoke；API AI 已验证跨 Worker DO binding | 生产规模 bundle/CPU/内存/cold-start/外连预算 |
| CF-03 Better Auth | 部分完成 | 独立 D1、signup/login/session/JWT、staging 浏览器登录 | OAuth、link/delete、导入 checksum、JWKS rotation、abort/retry、生产身份连续性 |
| CF-04 R2 | 部分完成 | PUT/GET/range/conditional/checksum/delete smoke | multipart、presigned expiry、迁移中断重放、全量 residual/cutover |
| CF-05 App D1 | 选定 route group 已验证 | 用户设置、conversation、memory、recap/chat 等投影路由 | 生产规模回放、账户级 authority cutover、全量领域迁移 |
| CF-06 Queues/Workflows | 部分完成 | transcription Queue、幂等/重复/冲突/终态、DLQ 相关验证 | Workflows、高风险删除/finalization contracts、积压恢复与 replay 证据 |
| CF-07 Redis primitive 拆分 | 部分完成 | staging 新 Worker 不接 Redis；当前 Cloudflare-owned 的 14 条通用限流路由/9 个 policy 与桌面 TTS 20/min+50k chars/UTC-day 细粒度额度已进入独立 rate-limit Durable Object，并覆盖并发、TTL、日切、boost/shadow、429、fail-open/fail-closed；部署依赖保持 `rate-limit → api-ai → edge` 单向 | 尚未迁移的 legacy route policy，以及 KV cache、锁/连接状态和 Queue primitive 仍待逐 family 迁移 |
| CF-08 Realtime + ASR | 部分完成 | Realtime DO 协议/WebSocket、Workers AI ASR/STT/TTS/翻译 smoke | 多语言/噪声 WER、首字/final p50/p95/p99、重连、成本、区域与 cohort |
| CF-09 Vectorize | 部分完成 | embedding seam/768 维路径、现有检索约束已记录 | namespace/backfill/recall/hydrate/delete；3072 维 screenshot embedding 仍不能直接迁移 |
| CF-10 其余 API 领域 | 部分完成 | 已迁移并声明 owner 的 core/ai 路由 | 大量 legacy route、Firebase/Redis/同步 SDK/复杂 fan-out 仍待领域化改造 |
| CF-11 Web/vinext | staging 已验证 | Web Worker build/deploy、login/recaps/chat/conversations 等浏览器路径 | 生产域名、生产身份、Firebase 连续性和完整 E2E 门槛 |
| CF-12 生产数据迁移 | 未开始 | 无生产数据切换证据 | snapshot/checksum、规模回放、备份恢复、账户 cutover/rollback、DR |
| CF-13 生产切换/清理 | 未开始 | deploy 脚本明确只允许 staging | 1%→5%→25%→50%→100% rollout、on-call/SLO/cost、legacy cleanup |

## 本轮验证证据

- Cloudflare TypeScript：15 个文件、117 个测试通过。
- `api-core`：110 个测试通过；`api-ai`：37 个测试通过。
- Web：7 个测试文件、25 个测试通过。
- manifest：215 条 Cloudflare 路由、577 条完整 backend 路由和 15 个 staging 资源通过校验；380 条 legacy-owned 路由成为可量化迁移队列。
- `npm run deploy:staging`：健康检查、迁移和 smoke 全部通过；Edge health 为 200。
- 浏览器 staging：`/recaps`、`/chat`、`/conversations`、`/memories`、`/my-apps`、`/tasks`、`/settings` 均无新的 API error/404。
- 已验证的直接接口：daily summaries 200、messages GET/DELETE 200、未认证访问 401、未知 summary 404。
- `POST /v2/messages` 当前返回明确的 `503 provider_not_configured`，这是聊天 provider 尚未配置的已知能力缺口，不是路由 404。

## 发布前必须补齐

1. 完成 Better Auth 全生命周期和生产身份连续性验证。
2. 完成 R2 全量对象迁移、checksum/residual、multipart/presigned 和中断恢复演练。
3. 为每个 Redis key family 选择 KV/DO/Queues owner，并完成并发、TTL、锁故障测试。
4. 补齐 Workflows 和高风险异步任务 contract，包含 DLQ/replay/backlog recovery。
5. 建立 ASR 质量/延迟/成本基线和 Vectorize projection/recall/hydrate 证据。
6. 逐领域迁移剩余 legacy routes，完成生产规模 D1/数据迁移与灾备演练。
7. 通过生产 rollout、监控/告警/成本预算和 legacy cleanup 门槛后，才切生产 DNS/identity。

权威设计约束和完整验收矩阵见 [`dev/cloudflare-adaptation-plan.md`](../../dev/cloudflare-adaptation-plan.md)。
