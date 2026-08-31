# External MCP app migration gates

截至 2026-09-01，远端 MCP app 的三条 exact 路径与 Twitter ownership exact route 已由 Edge → Jobs 承载 staging owner；`migrate-owner` 仍是 `legacy-owned`。本文件描述当前 owner seam 及把“远端 MCP server 安装为 Omi app”迁移到生产前必须闭合的契约：

| 路由 | legacy 行为 | 当前结论 |
| --- | --- | --- |
| `POST /v1/apps/mcp` | 发现 OAuth metadata，动态注册 client，生成 PKCE，写入 pending app；无 OAuth 时直接 discovery 并创建 app | staging 已由 Jobs owner 承载；使用 D1 app/connection authority、AES-GCM credential envelope 和 bounded provider adapter |
| `GET /v1/apps/mcp/callback` | 一次性 state/code exchange，刷新 app token，带 token 做 MCP `initialize`/`tools/list`，写 app/cache，返回 HTML | staging 已由 Jobs owner 承载；exact callback auto-discovery/install，state 只允许一次消费 |
| `POST /v1/apps/{app_id}/mcp/refresh` | 按 owner 重新 discovery；401 时 refresh token，再写 token/tools/cache | staging 已由 Jobs owner 承载；owner/revision CAS + token rotation 后复用 bounded discovery |
| `POST /v1/apps/migrate-owner` | Firebase anonymous source token 证明旧 owner，再把旧 owner 的 Firestore app 迁到目标 uid | 保持 legacy；Better Auth uid 不能证明 Firebase anonymous identity |
| `GET /v1/personas/twitter/verify-ownership` | RapidAPI timeline 最新 tweet 验证；按 Firebase provider 决定新建 Persona 或关联已有 Persona | 保持 legacy；Twitter verification 与已有 X OAuth connection 不是同一 authority |

`migrate-owner` 现在有一个不加入 legacy manifest 的 Edge→Jobs dormant seam：
`0122_app_owner_migration.sql` 保存已由可信导入流程核验的
`firebase-anonymous` source projection 和 hash-only proof，并以
`cf_app_owner_migration_jobs` 记录 source/target、target account generation、
幂等键、lease、retry 和结果。`POST /v2/cf/apps/migrate-owner` 只有在两个
显式 staging gate、目标 D1 cutover 已完成、source projection=`imported` 且
proof hash 匹配时才会入队；source proof 不接收 Firebase token，也不从
`old_id` 推断身份。Jobs consumer 会在执行前重复校验 source revision、目标
generation 和双方删除 fence，并将 D1 投影结果纳入同一 lease/retry 生命周期。
默认 gate 仍关闭，因此这不代表 exact legacy owner 已切换或已完成
Firestore app/memory re-encryption。

0127 又把 Firebase identity proof 与 source data projection 分开：身份投影默认只写
`data_projection_status=unverified`，不能直接作为 app-owner migration 的数据准入。
只有经过审阅的 Firestore export/import workflow 写入 content-bound
`data_projection_revision` 后，Jobs 才会接受该 source；`memory_projection_count=0`
必须明确标记 `memory_reencryption_status=not_required`，非零 memory 则必须有独立的
`memory_reencryption_revision` 和 `completed` 状态。缺少这些证据时 admission 返回
`503 source_data_projection_not_admitted`，不会创建 Queue job 或修改 D1 app owner。

审阅后的投影证据通过独立的 `POST /v2/cf/apps/migrate-owner/data-projection`
写入。该 writer 只接受受限的 hash-only source proof、content-bound revision、行数和
memory re-encryption 结果，使用 `APPS_ADMIN_KEY` 对应的 `secret-key`，并受独立的
`APP_OWNER_MIGRATION_DATA_ATTESTATION_STAGING_ENABLED` gate 保护（默认关闭）。它不
接受 Firebase token、raw Firebase UID 或 Better Auth session，也不是面向普通用户的
迁移入口；写入使用 source/proof/target generation CAS，完成后的 revision 不可改写。
因此 Persona/Chat planner 或 Firestore export/import operator 必须先产出并审阅这些
content-bound revisions，再调用 writer；当前仓库没有把 planner 结果自动提升为 attestation
的内部 CLI，未完成该流程时 exact route 仍保持 legacy。

当两个 gate 显式开启时，Jobs executor 只迁移已完成数据投影且
`cf_app_catalog.owner_uid` 等于 hash-only `fb-anon-<sha256>` source reference
的 D1 app rows。每个 row 写入 target account generation 和 migration job marker，
并以 owner/source/job CAS 防止重复或跨账号转移；已知 MCP connection、discovery、
pending OAuth transaction 和 app payment projection 会按 app id 同步转移（pending
OAuth transaction 会失效），账号删除 fence 由 D1 trigger 和执行前检查共同保护。
source projection 的 `app_projection_count` 必须与 D1 catalog 行数完全一致，否则
任务终止并保留 fail-closed 状态。数据投影与 memory re-encryption 证明在执行前和
执行时都会重复校验；旧 source 证明一旦完成不可被重写。没有通过独立
re-encryption 证明时，任务会 fail-closed，绝不伪造 Firestore re-encryption；通过后
结果只报告 `completed` 或 `not_required`，并带上对应 revision。

## Foundation schema and route authority

迁移 `0112_mcp_app_authority.sql` 增加最小 authority foundation；exact legacy owner 的路由切换由 manifest 和两个显式 staging 开关控制，不会隐式改变生产 owner：

- `cf_mcp_app_connections`：`app_id + owner_uid` 的上游 MCP URL、OAuth metadata、授权状态、revision；`credential_envelope_enc` 只允许 `v1.*` AES-GCM envelope，不能放 plaintext client/access/refresh token。
- `cf_mcp_app_oauth_transactions`：`SHA-256(state)`、加密 PKCE verifier、加密 registration credentials、固定 redirect/token/authorization endpoint、expiry、single-use status。callback 必须使用 `DELETE ... RETURNING` 或等价的 `status='pending' AND expires_at > now` CAS，不能按 app 查询后再更新。
- `cf_mcp_app_discoveries`：最后一次成功的 endpoint/protocol/tools JSON 与 revision。provider 失败只能写 `failed/last_error`，不能覆盖最后一个成功的 tools projection；成功后才允许刷新公开目录/cache。

当前已增加一个显式开关的 namespaced staging seam，以及复用该 provider adapter 的 exact legacy staging owner：

- `POST /v2/cf/apps/mcp/authorize`：Better Auth signed context 下校验 owner-scoped app/provider endpoints，执行可选 RFC 7591 registration，生成 S256 PKCE 和一次性 state，并把 verifier/registration secret 以 AES-GCM envelope 写入 D1。
- `GET /v2/cf/apps/mcp/callback`：按 hash-only state 原子消费 transaction，禁止 provider 自动 redirect，bounded 读取 token response，凭据只写入加密 connection envelope；重复/过期 state、跨 owner、私网/映射地址、超限或 malformed provider payload 均 fail-closed。
- `POST /v2/cf/apps/mcp/discover`：Better Auth owner 读取已授权的加密 connection，按受限 endpoint candidates 尝试 MCP transport；streamable HTTP 发送 bounded `initialize`、`notifications/initialized`、分页 `tools/list`，对 404/405 安全回退旧 SSE endpoint 流；响应必须是严格 JSON-RPC 2.0，游标循环、重复工具和超限 payload 均 fail-closed。通过 `Mcp-Session-Id` 续接并以 owner/revision CAS 写入 `cf_mcp_app_discoveries`；401 会标记 `reauthorize`，不会把 provider token 写入 app catalog。
- `POST /v2/cf/apps/mcp/refresh`：Better Auth owner 读取并解密 connection envelope，通过禁止 redirect、20 秒 timeout 和 bounded response 的 refresh-token grant 更新凭据；以 connection revision CAS 防止并发 refresh 双写，provider 401 会清空 envelope 并转为 `reauthorize`，成功后立即复用 bounded discovery。该入口只属于 namespaced staging seam，不改变 legacy refresh owner。
- `POST /v2/cf/apps/mcp/install`：仅允许 authorized connection 且 discovery=`ready` 的 owner，将 app 原子投影进现有 `cf_user_enabled_apps`；重复安装幂等，不伪造 provider token。
- `GET /v2/cf/apps/mcp/tools`：API Core 只读当前 Better Auth owner 已安装且 discovery=`ready` 的 MCP apps，按 D1 owner/status/revision 重新校验并 bounded 投影 tool name/description/inputSchema；不返回 provider endpoint、OAuth metadata 或凭据，discovery 失败时不会提供 stale tools。它是后续 chat/tool runtime 的只读输入，不执行上游工具。
- `POST /v2/cf/apps/mcp/tools/{appId}/call`：Jobs 只允许同一 Better Auth owner 已安装且 ready 的 app，严格校验 tool name、JSON object arguments 和 24KB request/16KB arguments 上限；在 Worker 内解密凭据后发送标准 MCP `initialize`→`notifications/initialized`→`tools/call`，支持已有 SSE fallback，响应 bounded 且不回显凭据。D1 account-deletion fence 和 connection revision CAS 防止删号期间发起调用或把 401 旧凭据状态写回；调用结果不持久化，仍不是 legacy chat runtime 的 alias。
- `0115_mcp_app_oauth_generation.sql` 把 connection 绑定到具体 transaction generation，避免慢 callback 覆盖更新中的授权。

### Exact legacy staging owner

`MCP_APP_EXACT_LEGACY_STAGING_ENABLED=true`（Edge）与
`MCP_APP_LEGACY_EXACT_STAGING_ENABLED=true`（Jobs）时，三条 exact route
由 Jobs owner 处理，并复用上述 D1/HTTPS/PKCE/discovery/refresh adapter：

- `POST /v1/apps/mcp` 接收旧的 `{name, mcp_server_url, description}` body；Jobs 创建 owner-scoped D1 catalog row，读取 OAuth metadata，执行 RFC 7591 registration + S256 PKCE，或对无 OAuth server 直接做匿名 MCP discovery。动态 app id、OAuth state、client credentials 和 provider token 均不会写入 legacy Firestore 或 URL。
- `GET /v1/apps/mcp/callback` 使用 exact callback URI，原子消费 hash-only state，并在 token exchange 后自动执行 bounded discovery/install，返回旧客户端需要的 HTML completion page。
- `POST /v1/apps/{app_id}/mcp/refresh` 从 path 读取 app id，按 owner/revision CAS refresh provider token，再返回 legacy `{tools_count, tool_names}` envelope。

这是真正的 staging owner boundary，不是生产 wire parity 声明：exact routes 当前使用 Better Auth signed context；provider secret 未配置时会由 Jobs 返回 `503 mcp_app_oauth_unavailable`，不会 fallback 到 legacy。旧 Firebase bearer continuity、历史 Firestore app/catalog backfill、logo/cache side effects、真实 staging provider replay 与 production cutover 仍未完成。

namespaced seam 需要 `MCP_APP_OAUTH_STAGING_ENABLED=true`；exact legacy seam 需要 `MCP_APP_LEGACY_EXACT_STAGING_ENABLED=true`；两者在涉及 token envelope 时都需要 `MCP_APP_TOKEN_ENCRYPTION_SECRET`（至少 32 字节）。实现已闭合 registration→authorization redirect→token exchange→严格 JSON-RPC discovery（分页、候选 endpoint、旧 SSE fallback）→bounded token refresh→authorized install projection→owner-scoped ready tools projection→显式 tools/call execution；provider revoke、真实 staging provider replay、旧 Firebase auth、历史 Firestore catalog/cache backfill、生产 cutover 尚未完成。

这三张 MCP 表均带 `owner_uid`、INSERT/UPDATE account-deletion fence 和 owner/status/expiry 索引；残留扫描与 purge inventory 已登记。`migrate-owner` 的独立 migration job 表同样只保存 hash-only proof 和生命周期元数据，不把 Firebase token、Firestore payload 或 R2 logo 数据伪装成已迁移 authority。

## Provider fixture

[`tests/fixtures/mcp-app-oauth-contract.json`](../../deploy/cloudflare/tests/fixtures/mcp-app-oauth-contract.json) 固化无真实 secret 的 wire fixture：RFC 7591 registration、authorization query（含 `code_challenge_method=S256`）、token response、MCP `initialize` 与 `tools/list`。fixture 同时列出 invalid/expired/replayed state、PKCE mismatch、cross-owner、401 refresh/discovery failure、strict JSON-RPC mismatch、cursor pagination、endpoint candidate 和 oversized response 等失败类。

它目前是准入 fixture，不是 live provider 成功证明。切换生产 owner 前必须用同一 fixture 驱动实际 Worker adapter，并增加真实 staging MCP server 的 registration→callback→discovery→refresh 回放；不能只测试 D1 insert、exact route forwarding 或 `refresh-manifest`。

## Owner cutover gates

在这五条路径切换 owner 前，必须同时具备：

1. Better Auth/Firebase identity bridge：`uid`、anonymous source proof、删除/撤销状态可回放且不可跨账号使用；`migrate-owner` 的 source token 不得仅凭 URL body 或 providerless user record 放行。
2. HTTPS-only、bounded metadata/registration/tool response，以及 SSRF/redirect policy；provider redirect、DNS/private address 和 malformed JSON 均有失败 fixture。
3. D1/Jobs transaction：registration、PKCE、callback single-use、refresh lease/retry、discovery revision/CAS 与 app owner 校验必须在同一 authority；上游 401、token rotation、重复 callback 和并发 refresh 结果确定。
4. Secret lifecycle：凭据使用 Workers secret-derived AES-GCM envelope，日志/HTML/目录 projection 不泄露 token；账号删除必须清理 D1 envelope、pending transactions、R2 logo/cache 和 provider revoke（如 provider 支持），并有 residual=0 证据。
5. Wire conformance：成功与失败 HTTP/HTML/JSON 形状、MCP `initialize`/`tools/list`/session header、tool schema 序列化、RapidAPI verification response 均与旧客户端/provider fixture 对齐。
6. Staging live proof：认证 owner 命中新 handler、真实 provider 正向/失败/重放探针完成、旧 backend 未被调用、跨 uid 与删号探针完成；provider secret 缺失时也必须保持 `503` 且不回源 legacy。

## Existing Cloudflare surfaces that are intentionally not aliases

`/api/better-auth/*` 与 `/v1/mcp/*` 的 OAuth/session surface 是“外部 MCP client 调用 Omi MCP server”，其 `oauthClient`、grant 和 access token 不能承载“用户安装外部 MCP server”所需的 upstream client registration/token。当前 `/v1/apps/:app_id/refresh-manifest` 只读取 `chat_tools_manifest_url`，不执行 OAuth、token refresh、MCP JSON-RPC discovery；`/v1/personas/twitter/profile` 也只读 RapidAPI profile，不执行 tweet ownership proof 或 Persona mutation。因此这些入口保持独立 owner，不应通过 manifest alias 宣称迁移完成。
