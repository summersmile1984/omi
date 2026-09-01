# Remaining legacy route contract gates

截至 2026-09-01，Cloudflare route inventory 已没有 `legacy-owned` 路由。本文件记录本轮对 auth/oauth、phone、wrapped、task intelligence、chat compatibility 和 Persona/MCP 相关入口的独立审计结果。它是迁移准入清单，不是把 legacy 路由改成一个返回成功的兼容别名。当前按“空数据新部署、无旧客户端”验收：历史回填和旧 wire parity 不属于本期完成条件；本期只需闭合新客户端会实际使用的 authority、删除和 live smoke。Workers AI 是唯一的 AI provider；OpenAI/Gemini secret、BYOK 和 Assistants 兼容面不属于本期依赖，也不应配置。只有用户明确启用 Google/Apple、Calendar、Twilio 等业务集成时，才需要对应业务 provider credentials。

## 结论

Auth/social、External App OAuth 与 Persona/Twitter 已切换到 Cloudflare staging owner；当前没有 `legacy-owned` 路由。以下表格记录新部署仍需按功能启用的 provider/live 验证；历史数据和旧协议只作为后续窗口记录：

| 路由组 | 条数 | 当前判断 | 不能切换的硬门槛 |
| --- | ---: | --- | --- |
| Auth / social | 0 | 新部署可用；业务 OAuth 可选 | Better Auth email/session 已闭合；只有启用 Google/Apple 登录时才需要对应业务 provider credentials 和 callback smoke |
| External App OAuth | 0 | 新部署可用；业务 OAuth 可选 | D1 app projection、CSRF transaction、install CAS 和删除 fence 已闭合；只有启用外部应用时才需要真实 provider 配置和 callback/revoke smoke |
| Phone / Twilio | 0 | 可选，待 provider 配置 | Jobs 已闭合 caller-ID、quota、webhook 和删除清理；只有启用电话功能时才需要 Twilio credentials/号码验证 |
| Wrapped | 0 | 可选，待 Workers AI/Queue smoke | D1 recap/job/notification authority 已闭合；使用 Wrapped 时只需验证 Workers AI binding 和 Queue drain，不需要 OpenAI/Gemini secret |
| Chat compatibility | 0 | Workers AI native contract 已用 | Jobs/API-AI bounded chat routes 只对新客户端承诺 Workers AI；OpenAI BYOK/Assistants adapter 是默认关闭的非目标兼容面，不需要配置也不计入完成阻塞 |
| Persona / MCP mutation | 0 | 可选，待 provider smoke | MCP registration/callback/refresh 与 D1 projection 已闭合；启用外部 MCP 时需 provider URL/secret 和一次 discovery/tool smoke |
| Staged tasks / task intelligence | 0 | 可选，待 Workers AI/Queue smoke | API Core/D1 candidate authority、Workers AI receipt 和 Queue retry 已闭合；使用该功能时需做 Workers AI/Queue 正向探针，不需要 OpenAI/Gemini secret |
| Gemini proxy | 0 | 非目标兼容面，默认关闭 | Gemini exact routes 保留 fail-closed boundary 以防误调用；不配置 Gemini secret 不影响任何 Workers AI canonical path，也不计入本期阻塞 |
| Files | 0 | 新客户端文本路径可用 | 通用文件上传与文本问答使用 R2/D1 + Workers AI；图片/PDF/二进制和旧 Assistants adapter 不属于本期目标，不需要 OpenAI provider |

上表当前 legacy 合计 0 条。`POST /v1/apps/migrate-owner` 已由 Edge→Jobs staging owner 承载，保留旧请求/响应形状但默认 gate 关闭，并要求 Auth anonymous bridge、数据 attestation、generation/deletion fence；它仍受真实 provider、身份连续性、历史回放和生产切换门槛约束。Auth/social 的 4 条 exact 路径与 External App OAuth 的 2 条 exact 路径已由 Auth/Jobs staging owner 承载，但同样不能视为 production parity；`GET /v1/apps/mcp/callback`、`POST /v1/apps/mcp` 和 `POST /v1/apps/{app_id}/mcp/refresh` 已由 Jobs staging owner 承载。Twitter ownership exact route 默认 gate/secret 关闭，生产 provider/data parity 仍保留在表内。`/v1/mcp/*` 和 `/api/better-auth/*` 已迁移的 MCP OAuth/会话入口也不计入本表。Task intelligence 的 13 条路径已是 staging owner，但其生产门槛仍保留在表内。

## Auth / social：不能用 Better Auth session 别名替代

Legacy 实现位于 [`backend/routers/auth.py`](../../backend/routers/auth.py)：

- `GET /v1/auth/authorize` 建立 5 分钟 Redis session，绑定 provider、原始 `redirect_uri`、state 和 PKCE；随后跳转 Google/Apple。
- `GET /v1/auth/callback/google` 和 `POST /v1/auth/callback/apple` 从 Redis 取一次性 session，交换 provider code，写一次性 auth code，并返回带有 `code/state/redirect_uri` 的 HTML 页面。Apple 首次授权的 `user` form 字段还要进入 auth-code payload。
- `POST /v1/auth/token` 一次性消费 auth code，校验 redirect URI/PKCE，并按客户端选项返回 OAuth credentials 和可选 Firebase custom token；已有客户端把这个 exchange 当作 Firebase 登录入口。

Cloudflare Auth Worker 目前的 `/api/better-auth/*` 使用 Better Auth D1/session/social provider contract；Better Auth 官方提供 Cloudflare Workers/Hono 与 D1 集成，因此这条新 surface 的运行时选择是可行的，但它仍没有证明以下 legacy 兼容关系。仓库已有 `scripts/import-firebase-identities.mjs` 和 `auth_identity_imports` ledger，可保留 Firebase UID 并导入 password/Google/Apple account；当前缺的是对真实生产导出的全量回放、冲突/撤销审计和旧 principal 覆盖证据：

1. Firebase UID 与 Better Auth user/account 的双向、可回滚 identity link/import；
2. Google/Apple provider callback 后的 legacy HTML/loopback/mobile redirect；
3. Firebase custom-token 签发、旧客户端解析和撤销语义；
4. legacy Redis session/auth-code 的一次性消费、PKCE 和删除 fence。

以上证据已足以支持 staging owner，但只有完成 identity import ledger、provider link replay、兼容 auth-code 表/lease、Firebase credential bridge 和客户端 conformance test 后，才允许进行生产切换。Better Auth social sign-in 本身不能作为生产兼容证明。

## External App OAuth：MCP OAuth 不是同一个 authority

Legacy 实现位于 [`backend/routers/oauth.py`](../../backend/routers/oauth.py)：

- `GET /v1/oauth/authorize` 读取 Firestore app、capabilities、external integration 和 Firebase config，生成 CSRF cookie，并返回 consent HTML。
- `POST /v1/oauth/token` 校验 Firebase ID token 与 CSRF cookie，读取 app 的 private/tester/paid/setup 状态，必要时 enable app/increment installs，最后返回 `{uid, redirect_url, state}`。

现有 D1 `cf_app_catalog` 和 `/v1/mcp/oauth/*` 只覆盖 Cloudflare app projection 与 MCP server 的 OAuth grants。`0116_external_app_oauth.sql` 与 `/v2/cf/oauth/*` namespaced seam 已覆盖 Better Auth uid、app revision、setup/paid/tester admission、hash-only CSRF transaction、install CAS 和 deletion fence；Jobs 现在复用这套 authority 承载精确 `/v1/oauth/*` staging owner，但仍不等于历史 Firestore catalog 已回填或生产 Firebase token/旧客户端 wire parity 已完成。因此不能把 `/v1/mcp/authorize` 或 Better Auth session 直接当作生产 `/v1/oauth/*` 兼容证明。

生产切换前仍必须回放并核验：

- uid-scoped `cf_external_oauth_transactions`（state、PKCE/CSRF、app、redirect、expiry、single-use）；
- D1 app/payment/install reader 与 CAS writer；
- provider identity/token bridge 和旧客户端 response conformance；
- setup URL SSRF/redirect policy、删除 fence、跨 uid/过期/重放测试。

## Phone / Twilio：Workers 能调用 Twilio，但现在没有业务 authority

Legacy 实现位于 [`backend/routers/phone_calls.py`](../../backend/routers/phone_calls.py)：

- verify/check 使用本机 Firestore pending/verified 状态，并调用 Twilio caller-ID API；check 还必须防止另一个 uid claim pending number。
- list/delete 读写 Firestore phone numbers；delete 还要删除 Twilio caller-ID。
- token 要求 primary verified number，并生成 Twilio access token。
- TwiML webhook 读取有界 form、构造 canonical URL、校验 `X-Twilio-Signature`，再查询 uid 的 verified caller ID。

Workers 的 `fetch` 可以承载 Twilio REST 请求，但这不自动提供生产迁移。当前 Jobs owner 已落地 `0108_phone_twilio.sql`、pending verification 唯一约束/TTL、account-generation/deletion fence、Twilio secret lifecycle、原子 quota reservation、CallId/CallSid 幂等、签名 webhook 和 Voice token tests。仍需真实 staging Twilio credentials/号码验证、历史 Firestore 回填和 production cutover；在这些门槛完成前不能声称完整 production parity。

## Wrapped：必须先定义结果 authority 和可重放 job

Legacy 实现位于 [`backend/routers/wrapped.py`](../../backend/routers/wrapped.py) 以及 `backend/utils/wrapped_analysis.py`：读取 Firestore `users/{uid}/wrapped/{year}`，生成时聚合 conversations/action items，调用 `wrapped_analysis`，通过本机 `llm_executor` 和通知路径异步落结果。

当前 Jobs 已有 Wrapped-specific `cf_wrapped_jobs` result/job 表、bounded conversation/action-item D1 projection、结构化 Workers AI schema、Queue lease/retry、notification outbox、幂等键、删除 fence 和旧客户端 poll/response shape。仍需历史 Firestore result/source 回填、真实 staging provider probe 和 production cutover；仅对 completed destination-bound D1 projection 账号开放，不能把该 staging owner 误记为历史数据 parity。

## Chat compatibility：现有新 API 不能替换旧桌面协议

Legacy 入口为：

- `POST /v1/chat/materialize-prompts`
- `POST /v2/chat/materialize-prompts`
- `POST /v2/chat/completions`

这些入口的历史语义依赖旧 Firebase 身份、Firestore chat/materialization continuity，以及桌面端 provider、BYOK、Redis quota、tools、流式 SSE/usage/error 语义。当前 Cloudflare Jobs/API-AI 已提供 exact staging owner，但 D1 `cf_chat_sessions`、`/v2/chat-sessions` 与无状态 `/v2/chat/generate-reply` 仍是受限新 contract：后者不创建 chat session/message，也不提供旧 completions 的完整流式 wire parity。

Cloudflare 侧现已提供 `deploy/cloudflare/scripts/backfill-d1.mjs` 的 session/message 输入校验：只允许白名单列、256KiB 以内 JSON、`human/ai` 消息类型、uid/session 绑定和幂等 upsert，并拒绝 token、密钥和授权字段。要完成 production cutover 仍必须实际回放并核验 session/message reader，再锁定 provider selection/BYOK/quota/tool invocation/SSE schema 的 conformance fixtures；materialization 还需明确 D1 prompt authority、proactive intent 和跨设备幂等。当前 staging owner 只证明 bounded boundary，不等于旧桌面协议 parity。

## Persona / MCP mutation：已有 manifest refresh 不是 legacy refresh

本组的逐路由 authority foundation、wire fixture 和切换门槛见 [`mcp-app-migration-gates.md`](mcp-app-migration-gates.md)。MCP registration/callback/refresh、Twitter ownership exact route 与 app-owner exact adapter 已由 Jobs staging owner 承载；生产切换仍需 provider/Firebase identity 与历史 Firestore 回放。

`POST /v1/apps/{app_id}/mcp/refresh` 的 legacy 实现位于 [`backend/routers/apps.py`](../../backend/routers/apps.py)：它读取 Firestore `external_integration.mcp_server_url` 和 OAuth token，必要时刷新 token，再向 MCP server 做 authenticated tool discovery，并更新 `chat_tools` 和 public app cache。

Cloudflare 当前 `POST /v1/apps/{app_id}/refresh-manifest` 只针对 D1 中的 `chat_tools_manifest_url` 做 bounded HTTPS JSON fetch；Jobs app mutation 明确拒绝 `mcp_server_url` / `mcp_oauth_tokens` 写入，避免把 provider secrets 当普通 catalog JSON 存储。两个 endpoint 的输入、认证、token refresh、discovery、错误和返回 shape 不同，不能只做路径 alias。要迁移需要专门的加密/provider-token authority、MCP discovery adapter、CAS/cache invalidation、secret deletion 和 OAuth callback 回放。

`POST /v1/apps/mcp`、`GET /v1/apps/mcp/callback` 和 `POST /v1/apps/{app_id}/mcp/refresh` 已具备 dynamic registration/PKCE、加密 credential、provider discovery、CAS 和 deletion fence 的 staging contract；真实第三方 MCP callback replay 仅在用户启用该业务集成时才需要，Firebase identity bridge、历史 Firestore app catalog 回放和 production cutover 不属于本期空数据目标。

为 owner migration 补了一个独立的 namespaced identity projection seam：`POST /v2/cf/apps/migrate-owner/identity-projection` 由 Jobs 调用 Auth 的 Identity Toolkit 验证，Auth 只返回 HMAC 派生的 `source_ref/source_uid_hash/source_proof_hash`，Jobs 在 D1 `0123_firebase_anonymous_identity_projection.sql` 中绑定目标 Better Auth generation、过期时间和删除 fence。它不保存 Firebase token，也不把 raw Firebase UID 写入 App D1；`FIREBASE_IDENTITY_PROJECTION_STAGING_ENABLED=false` 时保持 503。这个 seam 只完成 source-proof admission，尚未实现 Firestore app/persona/memory 回放、memory re-encryption 或 exact owner cutover。
`0127_app_owner_data_projection_attestation.sql` 进一步要求独立的 content-bound data projection revision；身份投影本身保持 `unverified`，不会创建 owner migration job。memory 数量为零时必须显式声明 `not_required`，否则必须提供 re-encryption revision 和 `completed` 证明，缺证据时返回 `source_data_projection_not_admitted`。

`POST /v1/apps/migrate-owner` 现在由 gated Edge→Jobs adapter 承载：旧 `source_token` 只进入 Auth anonymous identity bridge，随后必须通过已审阅的数据 projection、memory re-encryption 状态、generation 与 deletion fence 才会入队；默认 gate 关闭，不能绕过这些条件。Twitter exact route 的 D1 metadata projection 不等同于该 owner migration，也不提供 Firebase provider continuity。

## 可执行的迁移准入证据

任何一个阻塞组要切换 owner，提交必须同时包含以下证据：

1. route manifest 的 owner 变更与唯一 canonical handler；
2. D1/DO schema、account-generation/deletion fence、历史回放报告；
3. core success/error、跨 uid、重放/幂等、provider failure、retry/terminal、残留清理测试；
4. 旧客户端或旧 provider wire fixture（包括 redirect/HTML、form、SSE 或 token response，按路由适用）；
5. staging live probe：先证明新 owner 命中，再证明旧 backend 未被调用，最后完成真实正向/失败/删号探针。

在这些证据出现之前，`AUTH_OAUTH_STAGING_FAIL_CLOSED`、`PHONE_TWILIO_STAGING_FAIL_CLOSED`、`WRAPPED_STAGING_FAIL_CLOSED`、`CHAT_COMPAT_STAGING_FAIL_CLOSED` 和其它现有保护开关应继续保留；它们是泄露/误写防线，不计作生产迁移完成。已经切到 staging owner 的 Chat/Files/Gemini/MCP exact routes 仍需保持 provider/history fail-closed；缺少可选 provider 时只拒绝该可选请求，不影响 Workers AI 主路径。
