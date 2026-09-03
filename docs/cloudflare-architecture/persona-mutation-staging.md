# Persona 创建/更新 staging 边界

截至 2026-08-31，`POST /v1/personas` 与 `PATCH /v1/personas/{persona_id}` 已具备 Cloudflare staging owner 的最小闭环。Edge 使用 Better Auth 验证用户后，将请求绑定转发到 Jobs Worker；Jobs 不读取 Firebase/Firestore，也不依赖本机临时目录。

## 已闭合的行为

- 通过有界 multipart parser 接受 `persona_data` 和一个图片文件；图片大小、数量、字段、MIME magic bytes 和 JSON payload 均在 Worker 内校验。
- 图片先写入带 uid/app id 的 ASSETS R2 key；D1 `cf_app_catalog.owner_uid` 是 Persona metadata authority，写入失败会删除 staged R2 object。
- Persona id 根据 uid、规范化请求数据和图片摘要确定性生成；相同 multipart 重试返回相同 `app_id`，不会再次生成描述或留下重复对象。
- 生成描述使用 Workers AI；provider 缺失、异常或返回空内容时返回 503，并通过共享 fallback telemetry 记录，不写入半成品 catalog row。
- 用户名在 D1 catalog 中做冲突检查；响应保持 legacy `{"status":"ok","app_id":...,"username":...}` 形状。
- Account deletion 已覆盖 `cf_app_catalog.owner_uid` 和 `cf-app-logos/{uid}/` R2 前缀。
- PATCH 只允许更新 owner 自己的 D1 Persona projection；使用 `owner_uid + data_json` CAS 防止并发覆盖，用户名以 D1 冲突检查约束，省略图片时保留原 R2/legacy image URL，提供新图片时以版本化 R2 object 替换并清理旧对象。
- PATCH 的响应保持 legacy `{"status":"ok","app_id":...,"username":...}` 形状；D1 mutation fence 会阻止删除中的账户继续更新，失败的 staged logo 会清理或登记 `cf_asset_cleanup_tasks`。

## Twitter ownership staging owner

`deploy/cloudflare/migrations/app/0121_twitter_ownership.sql` 和 Jobs 的
`twitter-ownership.ts` 提供显式开关保护的 namespaced evidence seam，以及一个
可逐步启用的 exact staging owner：
`GET /v2/cf/personas/twitter/verify-ownership` 在
`TWITTER_OWNERSHIP_STAGING_ENABLED=true` 时才会读取有界的 RapidAPI
`timeline.php` 响应，验证最新 tweet 的
`Verifying my clone(<username>)` 短语，并把 request/provider response
fingerprint、account generation、结果和唯一 handle claim 写入 D1。
`cf_twitter_ownership_claims.handle` 的唯一约束防止跨 uid 抢占，transaction
request fingerprint 支持 replay 幂等，D1 INSERT/UPDATE triggers 和 residual
inventory 覆盖删号竞态。namespaced 路径只记录外部 provider evidence，不读取
或伪造 Firebase provider data。exact `/v1/personas/twitter/verify-ownership`
在 Edge 与 Jobs 的 `TWITTER_OWNERSHIP_EXACT_STAGING_ENABLED=true` 且 RapidAPI
secret 已配置时，额外读取同一 RapidAPI 的 bounded `screenname.php` profile，
并把 verified 结果以 owner-scoped CAS 写入 D1 `cf_app_catalog`（新建或显式
`persona_id` attach）。

该 seam 使用独立的 `/v2/cf/personas/twitter/verify-ownership` namespaced
Edge→Jobs 路径；exact route 已在 manifest 标记为 Jobs staging-owned，但默认
gate 为 false，关闭时返回 no-store `503`，非 staging 环境仍可回退到 legacy。
该 projection 只覆盖 Persona metadata 和 `twitter` account 字段，不伪造
Firebase identity、prompt/memory 生成或 legacy public-cache side effect；历史
Firestore 回放和 production positive probe 仍是生产准入 gates。

## 尚未宣称完成的部分

Twitter ownership exact route 与 app-owner exact adapter 已是可显式开启的 Jobs staging owner，外部 MCP app 的 exact registration/callback/refresh 也由 Jobs staging owner 承载（但 provider replay、Firebase identity/catalog backfill、memory re-encryption 和 production parity 仍未完成）。为防止 Persona/owner-migration 凭据或请求继续进入 legacy，Edge 在各自 exact gate 关闭时返回 `503 persona_apps_unavailable`，不读取请求 body 或调用 legacy；当前 manifest 已无 legacy-owned 路由。

Persona PATCH 的范围仍是“D1-owned projection 的 bounded update”，不是生产 wire parity：历史 Firestore Persona 尚未回填；现有 Workers AI 只生成有界 description，不能证明 legacy `generate_persona_prompt` 的 memories/conversations/tweets condensation 等价；legacy GCS logo 不能被 Worker 在没有迁移映射时主动删除。公开 app 目录目前直接读 D1、没有 Redis cache writer，故 PATCH 只能通过 D1 revision/`updated_at` 保持可见性，不能宣称已迁移 legacy cache invalidation。历史回填、完整 prompt contract、图片缩略图/legacy object mapping 和 production cutover 仍需独立门槛。

## 验证

`deploy/cloudflare/tests/persona-mutations.test.ts` 覆盖认证、图片校验、D1/R2 创建、Workers AI 描述调用、重复 multipart 幂等、无图更新、R2 logo rotation、跨 owner 拒绝和 deletion fence；Edge test 覆盖 PATCH 的 Better Auth→Jobs forwarding，manifest 与 Edge/Jobs route registration 同步更新。2026-08-31 本地 4 个 Persona mutation tests 通过；真实 Better Auth 账号创建默认 Persona 后，带 1x1 PNG 的 PATCH 经 staging 返回 `200` 并完成 username 更新，账号随后提交删号并收敛到 Persona residual `0`。历史 Firestore/prompt/cache parity、legacy GCS object mapping 和 production cutover 仍未完成。

`deploy/cloudflare/tests/twitter-ownership.test.ts` 覆盖 namespaced evidence 与 exact owner 的 feature gate、RapidAPI provider 缺失、profile/timeline D1 Persona 创建、显式 Persona attach、replay 幂等、verified handle 的跨 uid 冲突，以及 account-deletion fence/late-write 拒绝。Firebase provider continuity、prompt/memory side effects、历史回放和生产 positive probe 仍未完成。
