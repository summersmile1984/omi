# External MCP app migration gates

截至 2026-08-31，以下五条路径仍是 `legacy-owned`，本文件描述把“远端 MCP server 安装为 Omi app”迁移到 Workers 前必须闭合的契约：

| 路由 | legacy 行为 | 当前结论 |
| --- | --- | --- |
| `POST /v1/apps/mcp` | 发现 OAuth metadata，动态注册 client，生成 PKCE，写入 pending app；无 OAuth 时直接 discovery 并创建 app | 保持 legacy；尚无上游 token 与 discovery authority |
| `GET /v1/apps/mcp/callback` | 一次性 state/code exchange，刷新 app token，带 token 做 MCP `initialize`/`tools/list`，写 app/cache，返回 HTML | 保持 legacy；callback 是 unauthenticated，但必须由一次性 state 绑定 owner |
| `POST /v1/apps/{app_id}/mcp/refresh` | 按 owner 重新 discovery；401 时 refresh token，再写 token/tools/cache | 保持 legacy；不能复用 `refresh-manifest` |
| `POST /v1/apps/migrate-owner` | Firebase anonymous source token 证明旧 owner，再把旧 owner 的 Firestore app 迁到目标 uid | 保持 legacy；Better Auth uid 不能证明 Firebase anonymous identity |
| `GET /v1/personas/twitter/verify-ownership` | RapidAPI timeline 最新 tweet 验证；按 Firebase provider 决定新建 Persona 或关联已有 Persona | 保持 legacy；Twitter verification 与已有 X OAuth connection 不是同一 authority |

## Foundation schema（不接管路由）

迁移 `0112_mcp_app_authority.sql` 只增加最小 authority foundation，不会自动把任何 route 改成 Cloudflare owner：

- `cf_mcp_app_connections`：`app_id + owner_uid` 的上游 MCP URL、OAuth metadata、授权状态、revision；`credential_envelope_enc` 只允许 `v1.*` AES-GCM envelope，不能放 plaintext client/access/refresh token。
- `cf_mcp_app_oauth_transactions`：`SHA-256(state)`、加密 PKCE verifier、加密 registration credentials、固定 redirect/token/authorization endpoint、expiry、single-use status。callback 必须使用 `DELETE ... RETURNING` 或等价的 `status='pending' AND expires_at > now` CAS，不能按 app 查询后再更新。
- `cf_mcp_app_discoveries`：最后一次成功的 endpoint/protocol/tools JSON 与 revision。provider 失败只能写 `failed/last_error`，不能覆盖最后一个成功的 tools projection；成功后才允许刷新公开目录/cache。

当前已增加一个显式开关的 namespaced staging seam（不减少 legacy route 计数）：

- `POST /v2/cf/apps/mcp/authorize`：Better Auth signed context 下校验 owner-scoped app/provider endpoints，执行可选 RFC 7591 registration，生成 S256 PKCE 和一次性 state，并把 verifier/registration secret 以 AES-GCM envelope 写入 D1。
- `GET /v2/cf/apps/mcp/callback`：按 hash-only state 原子消费 transaction，禁止 provider 自动 redirect，bounded 读取 token response，凭据只写入加密 connection envelope；重复/过期 state、跨 owner、私网/映射地址、超限或 malformed provider payload 均 fail-closed。
- `POST /v2/cf/apps/mcp/discover`：Better Auth owner 读取已授权的加密 connection，向 public MCP endpoint 发送 bounded `initialize`、`notifications/initialized`、`tools/list`（支持 JSON/SSE 单响应），通过 `Mcp-Session-Id` 续接并以 owner/revision CAS 写入 `cf_mcp_app_discoveries`；401 会标记 `reauthorize`，不会把 provider token 写入 app catalog。
- `0115_mcp_app_oauth_generation.sql` 把 connection 绑定到具体 transaction generation，避免慢 callback 覆盖更新中的授权。

该 seam 需要 `MCP_APP_OAUTH_STAGING_ENABLED=true` 和 `MCP_APP_TOKEN_ENCRYPTION_SECRET`（至少 32 字节），默认关闭。它目前闭合 registration→authorization redirect→token exchange→受限 discovery；refresh、install、provider revoke、SSE 多候选 endpoint、真实 staging provider replay 尚未完成，因此不切换 `/v1/apps/mcp`、`/v1/apps/mcp/callback` 或 refresh owner。

这三张表均带 `owner_uid`、INSERT/UPDATE account-deletion fence 和 owner/status/expiry 索引；残留扫描与 purge inventory 已登记。基础 schema 不包含 migration job、Firebase proof 或 R2 logo cleanup，因为这些依赖仍未闭合，不能用空表伪造完成度。

## Provider fixture

[`tests/fixtures/mcp-app-oauth-contract.json`](../../deploy/cloudflare/tests/fixtures/mcp-app-oauth-contract.json) 固化无真实 secret 的 wire fixture：RFC 7591 registration、authorization query（含 `code_challenge_method=S256`）、token response、MCP `initialize` 与 `tools/list`。fixture 同时列出 invalid/expired/replayed state、PKCE mismatch、cross-owner、401 refresh/discovery failure 和 oversized response 等失败类。

它目前是准入 fixture，不是 live provider 成功证明。切换 owner 前必须用同一 fixture 驱动实际 Worker adapter，并增加真实 staging MCP server 的 registration→callback→discovery→refresh 回放；不能只测试 D1 insert 或 `refresh-manifest`。

## Owner cutover gates

在这五条路径切换 owner 前，必须同时具备：

1. Better Auth/Firebase identity bridge：`uid`、anonymous source proof、删除/撤销状态可回放且不可跨账号使用；`migrate-owner` 的 source token 不得仅凭 URL body 或 providerless user record 放行。
2. HTTPS-only、bounded metadata/registration/tool response，以及 SSRF/redirect policy；provider redirect、DNS/private address 和 malformed JSON 均有失败 fixture。
3. D1/Jobs transaction：registration、PKCE、callback single-use、refresh lease/retry、discovery revision/CAS 与 app owner 校验必须在同一 authority；上游 401、token rotation、重复 callback 和并发 refresh 结果确定。
4. Secret lifecycle：凭据使用 Workers secret-derived AES-GCM envelope，日志/HTML/目录 projection 不泄露 token；账号删除必须清理 D1 envelope、pending transactions、R2 logo/cache 和 provider revoke（如 provider 支持），并有 residual=0 证据。
5. Wire conformance：成功与失败 HTTP/HTML/JSON 形状、MCP `initialize`/`tools/list`/session header、tool schema 序列化、RapidAPI verification response 均与旧客户端/provider fixture 对齐。
6. Staging live proof：认证 owner 命中新 handler、真实 provider 正向/失败/重放探针完成、旧 backend 未被调用、跨 uid 与删号探针完成；在此之前继续使用 `PERSONA_APPS_STAGING_FAIL_CLOSED=true`。

## Existing Cloudflare surfaces that are intentionally not aliases

`/api/better-auth/*` 与 `/v1/mcp/*` 的 OAuth/session surface 是“外部 MCP client 调用 Omi MCP server”，其 `oauthClient`、grant 和 access token 不能承载“用户安装外部 MCP server”所需的 upstream client registration/token。当前 `/v1/apps/:app_id/refresh-manifest` 只读取 `chat_tools_manifest_url`，不执行 OAuth、token refresh、MCP JSON-RPC discovery；`/v1/personas/twitter/profile` 也只读 RapidAPI profile，不执行 tweet ownership proof 或 Persona mutation。因此这些入口保持独立 owner，不应通过 manifest alias 宣称迁移完成。
