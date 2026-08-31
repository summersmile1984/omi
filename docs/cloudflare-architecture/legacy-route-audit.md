# Legacy 路由迁移审计

截至 2026-08-31，`backend-routes.json` 中还有 60 条 `legacy-owned` 路由。这个清单不是把路由简单改成 Cloudflare 代理：只有当数据 authority、认证边界、异步重试和外部 provider 语义都能在 Workers 上闭合时，才允许把 owner 改成 `staging-owned`。

## 分组与迁移前置条件

| 路由分组 | 当前 legacy 条数 | 代表路径 | 主要缺口 | 下一步 |
| --- | ---: | --- | --- | --- |
| Auth / OAuth / social | 9 | `/v1/auth/*`、`/v1/oauth/*`、`/v1/apps/mcp/callback`、Twitter ownership | Firebase provider identity、Google/Apple callback、OAuth app consent 仍由 legacy 维护；Better Auth 目前只承接 MCP OAuth 和会话 | 先完成身份连续性与 provider link/import，再迁移 callback/token；不能用 Better Auth session 别名替代 Firebase exchange |
| Phone / external integrations | 6 | `/v1/phone/*`、电话 webhook | Twilio、Redis 状态、号码验证和电话 webhook | 定义 DO/Queue 状态机、签名校验和 provider secret 生命周期后再迁移 |
| Conversation lifecycle | 4 | `/v1/conversations/{id}/finalization`、`finalize`、`reprocess`、`merge` | legacy lifecycle path and fan-out | finalize/status/reprocess/merge now use shared D1 job projections, Jobs leases, and API Core; merge performs bounded R2 artifact copy before atomic source cleanup |
| Import / files / sync / audio | 9 | `/v1/import/*`、`/v1/files`、`/v2/files`、sync/audio jobs | GCS/本机临时文件、multipart、长任务和 R2 residual contract | `POST /v1/sync-local-files` 与 `POST /v2/voice-messages` 已复用 D1/R2/Queue 或 Workers AI authority；其余 import/files/audio jobs 仍需先完成 R2 multipart/presigned 与 Queue replay |
| Task intelligence / staged tasks | 15 | `/v1/staged-tasks*`、`/v1/task-intelligence/*`、`/v1/what-matters-now*` | candidate/recommendation store、generation fence、LLM judgment、device snapshot | candidate D1 projection 和 generation contract 完成前保持 fail-closed legacy owner |
| Persona / apps | 4 | `/v1/personas` mutation、`/v1/apps/*` MCP mutation、Twitter ownership | multipart 图片、R2、LLM prompt、Twitter provider identity 与 public app cache | 默认 Persona 和只读 profile 已迁移；通用 Persona mutation/Twitter ownership 需完整 D1/R2/provider contract |
| Memory admin / Archive | 3 | `/memory/admin/*`、`/memory/archive/search` | Archive capability | `/memory/search`、`/memory/vector/search` 与 D1 review queue 已迁移；archive/admin 仍需补齐 capability |
| Desktop release mutation/manifest | 0（路由已迁移，回填未完成） | release pipeline 回填和生产 Firestore→D1 回放 | 发布流水线凭据、历史 manifest 回放和生产切换审计 | immutable manifest、macOS Stable/Beta channel pointer、Beta admission/promotion/breakglass、legacy release bridge（`/updates/releases` POST/PATCH）和 cache-invalidation contract 已进入 D1；所有相关 endpoint 在 staging 由 API Core 提供，下一步回填发布流水线并复验 Beta 晋级族 |
| Metrics / wrapped / analytics | 3 | `/metrics`、`/v1/wrapped/*`、部分分析端点 | Prometheus/历史分析数据不在当前 D1 authority | 定义聚合与保留策略；不能以空响应冒充迁移完成 |
| Hume callback / provider webhooks | 1 | `/v1/agents/hume/callback` | 外部 webhook schema、幂等、长处理和重试 | 先落 Queue receipt 与 provider signature contract，再切 webhook owner |
| Data-protection migration | 3 | `/v1/users/migration/*` | 加密迁移写入、批量请求和 finalize 仍依赖 legacy storage | 先完成加密 payload、迁移 lease 和生产回放，再迁移写入端点 |
| Other legacy proxies | 3 | Gemini proxy、MCP owner migration 等 | provider credentials 与外部应用 owner continuity | 按 provider contract 单独迁移，保持 fail-closed |

## 当前可执行顺序

1. 让 release pipeline 使用 `.github/scripts/backfill-desktop-release-manifest.py` 回填已迁移的 D1 immutable manifest；Stable/Beta promotion、legacy release bridge 和 `clear-cache` 已由 API Core/D1 承接，完成 Firestore→D1 回放后再注入发布流水线凭据并复验 Beta 晋级。该工具只读 legacy manifest、验证 v1 digest，再向 Cloudflare Edge 的 `/v2/desktop/releases` 做幂等 POST，不改变 channel pointer。
2. 保持 conversation finalization/reprocess/merge、sync-local-files、voice-messages 与 account-deletion run 的 staging residual 监控；下一组迁移优先处理 `/v2/sync-jobs/run` 的内部触发边界和 release pipeline 回填。
3. 完成 candidate/recommendation authority 后，再处理 staged-tasks 与 What Matters Now；在此之前保持现有 404/legacy 边界。

Review queue 已完成第一阶段闭环：D1 canonical memory 写入会产生结构化冲突记录；三个 review-queue 端点由 API Core/Edge 承载；每次读取校验来源 `updated_at` revision 与 SHA-256 content hash，来源变化自动 tombstone；accept/reject/correct/timeout 解析具备 D1 原子写入和幂等状态。该阶段的 producer 覆盖 canonical `/v3/memories` 与 `/v3/memories/batch` 写入，手工已确认的 MCP/developer memory 不会重新进入队列。

本轮新增 `GET/POST /v1/agent/*`：API Core 只返回并执行已由 D1/Workers 实现的对话、记忆和任务工具，动态第三方 App 与未迁移的 provider 工具继续 fail-closed，避免把未实现的能力暴露给模型。每一组迁移都必须同时更新 route manifest、Edge owner、回归测试、删除/残留清理和 staging live evidence；不能仅添加同路径 alias 来降低 legacy 计数。

本轮新增 `POST /v2/chat/generate-reply`：API AI 使用 Workers AI 或已验证的 OpenAI BYOK 生成无状态草稿，历史只作为受限 prompt 输入，不创建聊天 session、不写入消息；D1 仅记录独立的 `v2_chat_generate_reply` 配额事件并在 provider 返回后结算，失败会关闭预留。Edge、manifest 和回归测试已同步更新。

本轮新增 `POST /v1/sync-local-files` 的 Cloudflare owner：旧路径现在由 Edge 转发到 Jobs，与 `/v2/sync-local-files` 共享 multipart staging、R2 对象、D1 content ledger、Queue lane、Workers AI ASR 和清理状态机；响应保留 `Deprecation: true` 与 v2 successor Link。v1 只接受可验证的 fresh capture，历史/无 provenance 的上传会安全返回 `503 backfill_capacity`，客户端应迁移到 v2 并按 job status 轮询；不再执行 legacy inline pipeline。

本轮新增 `POST /v2/voice-messages` 的 Cloudflare owner：API AI Worker 使用有界 multipart parser 与 Workers AI Whisper 完成转写，按 D1 fair-use source 去重计量，再把 transcript 交给 D1-backed `/v2/messages` 生成 SSE。空音频保持空 SSE；不支持的容器、超限和 provider/D1 故障均 fail-closed，不回落到本机临时文件或 legacy inline chat。

## Staging evidence（2026-08-31）

- 最近一次完整 staging 发布（2026-08-31）的 Worker 版本为：Auth `501c851c-b566-4f2e-bb0c-b63ed764f871`、Rate Limit `9c37a485-a7fb-4551-8afa-5ef15d773bed`、API Core `bbad2cb5-a5f9-4475-9678-42f44a73f2f5`、API AI `5fe35a6d-1555-4036-8c93-5d4e4451b6a7`、Realtime `12f65143-a75f-48bb-80e5-acd85474c6af`、Jobs `c0ad6a20-8a83-49d7-8f62-d60b6fc3757b`、Edge `f5d71850-731d-4207-b4bb-85cdee154c90`、Web `c84dbce5-6a56-4395-b9b9-f0458c32c60a`。
- `POST /updates/releases` 和 `PATCH /updates/releases/promote` 通过 Edge 实测返回 `401 Invalid or missing X-Release-Secret header`（不是 404），证明请求已进入 API Core 的新 owner；staging 当前尚未注入 `RELEASE_SECRET`，因此没有执行真实写入探针。
- `/health` 通过 Edge 实测返回 `200`；`POST /v2/desktop/beta/candidates/reserve`、`POST /v2/desktop/beta/promote-candidate`、`PUT /v2/desktop/beta/admission`、`POST /v2/desktop/beta/breakglass` 在缺少凭据时分别返回 `401`、`401`、`403`、`403`（均非 `404`）。staging 尚未注入 `BETA_PROMOTION_TOKEN`/`GITHUB_TOKEN`，因此没有伪造 Beta 正向 promotion 或 GitHub 证据结果。
- `npm run smoke:staging` 通过：Edge/v1 health、Apple association、OpenAI Apps challenge、公告、趋势、app reviews、支付边界均符合预期；authenticated checks 因未提供 smoke bearer token 而跳过。Calendar Google events 未认证探针返回 `401`（不是 `404`）。
- `/memory/vector/search?query=coffee&limit=3` 通过专用合成账号实测：未认证返回 `401`，Better Auth 认证后空 D1/Vectorize 结果返回 `200` 且 `search_status=ok`、`items=[]`；账号随后通过 `DELETE /v1/users/delete-account` 清理。此次发布版本为 API Core `50fb07d7-f464-43aa-a6ea-9560360c4280`、Edge `a991ab93-8013-40b9-8172-4a2049ffe5e9`。
- Memory review queue staging 数据面已实测：远端应用 `0093_memory_review_queue.sql` 后，API Core `e6e64b52-7215-44c7-9c91-0b5a9d91100a`、Edge `a39cd3d1-3524-472e-8935-8eeabc37e0a2`、Jobs `3f9efdd3-8695-4f16-8ca8-fdeb20df7342` 已发布；临时 Better Auth 账号通过 Edge 连续创建结构化冲突、读取到 `pending` queue、`accept` 得到 `accepted` 与确定性 D1 commit，并提交删号清理。未认证的三个 queue 路由均返回 `401` 而非 `404`。
- Conversation finalization 的 admission/status、D1 revision/job schema、Jobs lease/retry/reconcile 和 API Core Workers AI processor 已实现并有 Python/TypeScript 回归测试；已完成 staging migration 与正向队列验证，但不宣称生产等价。
- Conversation finalization staging live evidence（2026-08-31）：远端应用 `0094_conversation_finalization_jobs.sql` 后，API Core `cb1a8271-22f2-4211-9ba4-94d7463c4745`、Jobs `18df02b0-7ad1-4010-a063-46144d584899`、Edge `99eb710f-03c3-41bf-8f29-763630690393` 已发布。隔离 Better Auth 账号经 Edge 完成 `POST /v1/conversations/{id}/finalize`（200），status 实际经历 `queued → running → completed`（attempt 1），会话读回 `status=completed`；随后公开删号完成 Queue 清理，`cf_conversations` 与 `cf_conversation_finalization_jobs` 均为 0，仅保留预期 tombstone。
- Conversation reprocess 已完成 D1/Queue 闭环：共享 `cf_conversation_finalization_jobs` 新增 `operation/language_code/app_id`，Jobs 重试/租约恢复时保留参数；API Core 重跑 Workers AI enrichment、按来源替换旧 action/memory 派生行与向量删除/重建 outbox，并支持同一 processing job 幂等返回。staging 正向证据见下方。
- Conversation reprocess staging live evidence（2026-08-31）：`0095_conversation_reprocess_jobs.sql` 已远端应用；API Core `23cc4631-1942-40eb-b67d-e8c3186d0786`、Jobs `db0f4aa5-cff3-4be8-acd3-6e817f37ab91`、Edge `ebec0033-35af-4c27-8c08-9cee3078fe2c` 已发布。隔离 Better Auth 账号经 Edge 创建会话（200），提交 `POST /v1/conversations/{id}/reprocess?language_code=en&app_id=calendar-app`（200），status 实际经历 `running → completed`（attempt 1）；会话读回 `completed/discarded=false`，action item 派生数据可读，`external_data` 无内部 claim marker。随后 `DELETE /v1/users/delete-account`（200）完成，tombstone 存在且 conversation/action/memory/reprocess job/vector outbox/vector state 残留均为 0。
- Conversation merge 的 `0096_conversation_merge_jobs.sql` 已于 2026-08-31 应用 staging；API Core `9768ac95-9961-4b1f-ab74-05b788d1993c`、Jobs `6cdc9ccc-ba04-4a8a-8e6e-92d851223f0f`、Edge `839c648a-6c0a-4da5-872b-1ae75bcb1483` 已发布。隔离账号实测两个会话创建、merge admission `200/merging`、Queue 完成后结果会话 `completed` 且来源数为 2；随后删号完成，D1 conversation/merge job/action/memory/vector outbox 残留均为 0，仅保留 deletion tombstone。
- Calendar staging live recheck（2026-08-31）：本次 API Core `bf77c198-590a-4203-a4c3-64ba1122303a`、Jobs `855a508c-2f5c-4b4c-8b58-40a7923d5727`、Edge `47079a0a-d239-4890-8349-2c2c1c29d045` 已发布。隔离 Better Auth 账号经 Edge 完成 onboarding reset/skip/reset、Calendar integration 状态（underscore 与 hyphen alias）、meeting metadata create/list/get；未连接 events 为 400，OAuth URL 因 staging 未配置 client 为 503。Web Settings 点击连接后显示明确环境提示，浏览器控制台未出现 404；异步清理完成后 Auth account、Calendar onboarding/meeting/integration/OAuth state 与 deletion intent 均为 0，仅保留 1 条预期 tombstone。
- Sync v1 staging live recheck（2026-08-31）：Jobs `2a978062-cab4-48e3-88db-4da1a96ab600` 与 Edge `97acf6aa-ef87-4bdb-87d6-ed2107f53e6d` 发布后，隔离 Better Auth 账号经 Edge 调用 `POST /v1/sync-local-files`；未认证返回 `401`，无 capture provenance 的 multipart 上传安全返回 `503`、`code=backfill_capacity`、`Retry-After=30`，并保留 `Deprecation: true` 与 `/v2/sync-local-files` successor Link。探测账号随后通过 `DELETE /v1/users/delete-account` 返回 `200` 清理。
- Voice chat staging live evidence（2026-08-31）：API AI `efa4c5c7-85a0-4668-afa0-1c446014725c`、Edge `48fa6e53-fe68-4636-b87f-136784c34f83` 发布修复后，`POST /v2/voice-messages` 已声明 API AI/Workers AI owner；隔离 Better Auth 账号上传已知有声 WAV 返回 `200 text/event-stream`（987 字节 SSE），静音 WAV 按约定为空 SSE（0 字节），未认证返回 `401`。有界 multipart/容器校验、speech fair-use D1 source 去重和 transcript→chat SSE delegation 由 `test_voice_transcription_routes.py` 与 Edge forwarding test 覆盖，未配置或异常 provider 均返回稳定 4xx/5xx，不会启动 legacy pipeline；探针账号已提交删号。
- Calendar staging final probe（2026-08-31）：最新 Edge 健康 `200`；未认证 Google events 为 `401`，隔离账号 onboarding status 为 `200`，未连接 events 为业务态 `400`，OAuth URL 因 staging 未配置 client 为 `503`，页面不会出现路由 `404`；探针账号已提交删号。
- Agent tool directory staging live evidence（2026-08-31）：API Core `70bd49eb-f0db-4338-a166-0612af37eda7`、Edge `79ba9a42-e9fc-4819-b5ac-778487a5ee8b` 已发布；未认证 `GET /v1/agent/tools` 与 `POST /v1/agent/execute-tool` 均返回 `401`，Better Auth 认证后目录返回 `200` 与 7 个 Cloudflare-native 工具定义，执行 `get_memories_tool` 返回 D1 结果，未迁移的 `get_calendar_events_tool` 返回 `404`，响应不含 `config`，临时账号已提交删号。
- Stateless chat generate-reply staging live recheck（2026-08-31）：API AI `0b8636e3-ed1b-4928-8434-ac470110f238`、Edge `af9be7e8-277b-4392-9614-f19f72928ef7` 已发布。隔离 Better Auth 账号经真实 Edge 生成草稿返回 200；未认证、非法 history、缺失 app 分别返回 401/422/404。请求只产生独立 quota event，不写 chat session/message；公开删号完成后 quota/session/message/deletion intent 均清零，仅保留预期 tombstone。

## 本轮 authority 核对（2026-08-31）

- `POST /v1/import/limitless` 仍不能切到 Worker：legacy 会接收 multipart ZIP、落本机临时目录并启动后台解析；当前 D1 只有 `v3/memory-imports/batch` 的 artifact receipt，没有 ZIP 解包、导入 job、Queue 重试和同等的会话创建 authority。`retired_compat_routes.py` 中的 Limitless 删除接口是有意的零副作用兼容响应，不代表上传已迁移。
- `POST/PATCH /v1/personas*` 仍不能切到 Worker：创建/更新同时依赖图片上传、作者资料、用户名唯一化、Workers/legacy LLM prompt、以及公开目录缓存失效；当前 `cf_app_catalog` 仅是投影，直接写入会绕过这些约束。
- `/v1/oauth/*` 与 `/v1/apps/mcp*` 仍不能由 Better Auth session 直接替代：legacy token 路径验证 Firebase ID token、应用启用/付费状态及 OAuth state/PKCE；D1 的 MCP OAuth 表只覆盖已迁移的 `/v1/mcp/*`，不是外部应用动态注册流程。
- `/v2/sync-jobs/run` 仍是 legacy 内部触发路径：虽然 sync-local-files 已有 canonical conversation、lease、提取 fan-out 与 Queue consumer，但该手动 run boundary 尚未接入同一 Jobs admission contract。Merge 的外部 OAuth/AI fan-out 依赖仍按配置 fail-closed，不能把缺失 provider secret 当作成功。
- `/v1/users/account-deletion-wipes/run` 已切到 staging Jobs owner：Edge 先验证 Better Auth，再由 Jobs 从 D1 intent 反查 UID，只推进当前账号自己的 deletion intent；未知或跨账号 job 只返回 dropped，不执行任何写入。Cloudflare Queue consumer 仍是实际清理 authority，旧 Cloud Tasks/OIDC 生产 dispatcher 尚未切换。
- Desktop Beta 四条 mutation 已成组迁移：reserve/admission、signed GitHub evidence、manifest/pointer CAS 和 breakglass audit 共享 D1 控制面；没有 `BETA_PROMOTION_TOKEN`/`GITHUB_TOKEN` 时 promotion 与 emergency rollout 会 fail-closed，不会产生半写入。发布流水线凭据和历史 Firestore→D1 回放仍是 staging 正向验证前置条件。
