# Remaining legacy route contract gates

截至 2026-08-31，Cloudflare route inventory 仍有 40 条 `legacy-owned` 路由。本文件记录本轮对 auth/oauth、phone、wrapped、chat compatibility 和 Persona/MCP 相关入口的独立审计结果。它是迁移准入清单，不是把 legacy 路由改成一个返回成功的兼容别名；任何一项 wire contract、authority 或删除边界未闭合，都必须继续保持 legacy owner 或返回已记录的 staging fail-closed 错误。

## 结论

本轮没有发现可以安全地单独切换 owner 的最小组：

| 路由组 | 条数 | 当前判断 | 不能切换的硬门槛 |
| --- | ---: | --- | --- |
| Auth / social | 4 | 阻塞 | Firebase provider identity、Redis auth session/auth code、移动端 HTML callback、Firebase custom-token exchange 尚未由 Better Auth/D1 证明等价替代 |
| External App OAuth | 2 | 阻塞 | legacy Firestore app catalog、CSRF/state、app enable/payment/setup 检查和 `uid + redirect_url + state` 响应仍无同一 D1 authority |
| Phone / Twilio | 6 | 阻塞 | caller-ID 验证状态、Twilio API/token/webhook contract、quota 和删除清理没有 D1/DO/Queue authority |
| Wrapped | 2 | 阻塞 | Firestore recap 聚合、`wrapped_analysis` provider 输出、本机 executor、通知和 job/result 状态没有 Cloudflare 闭环 |
| Chat compatibility | 3 | 阻塞 | prompt materialization、desktop provider/BYOK/quota/tools/stream wire contract 与 D1 chat session 不是同一语义 |
| Persona / MCP mutation | 6 | 阻塞 | Firestore persona/app continuity、MCP OAuth token/discovery 和 public cache invalidation 尚未迁移 |
| Staged tasks / task intelligence | 13 | 阻塞 | candidate/recommendation authority、device/open-loop snapshot、LLM evaluation receipt 和 promotion transaction 尚未建立 |
| Gemini proxy | 2 | 阻塞 | Gemini/Vertex route、BYOK、Redis quota、SSE/usage/error 以及 cost accounting 尚未闭合 |
| Files | 2 | 部分准备 | Worker chat-file adapter 已有 D1/R2/provider contract，但 Assistants/session continuity、旧数据回填和下游 reader 仍未完成 |

上表合计 40 条。`GET /v1/apps/mcp/callback` 计入 Persona/MCP mutation，而不是 Auth / social；`/v1/mcp/*` 和 `/api/better-auth/*` 已迁移的 MCP OAuth/会话入口不计入本表。

## Auth / social：不能用 Better Auth session 别名替代

Legacy 实现位于 [`backend/routers/auth.py`](../../backend/routers/auth.py)：

- `GET /v1/auth/authorize` 建立 5 分钟 Redis session，绑定 provider、原始 `redirect_uri`、state 和 PKCE；随后跳转 Google/Apple。
- `GET /v1/auth/callback/google` 和 `POST /v1/auth/callback/apple` 从 Redis 取一次性 session，交换 provider code，写一次性 auth code，并返回带有 `code/state/redirect_uri` 的 HTML 页面。Apple 首次授权的 `user` form 字段还要进入 auth-code payload。
- `POST /v1/auth/token` 一次性消费 auth code，校验 redirect URI/PKCE，并按客户端选项返回 OAuth credentials 和可选 Firebase custom token；已有客户端把这个 exchange 当作 Firebase 登录入口。

Cloudflare Auth Worker 目前的 `/api/better-auth/*` 使用 Better Auth D1/session/social provider contract，能证明新会话 authority，但没有证明以下兼容关系：

1. Firebase UID 与 Better Auth user/account 的双向、可回滚 identity link/import；
2. Google/Apple provider callback 后的 legacy HTML/loopback/mobile redirect；
3. Firebase custom-token 签发、旧客户端解析和撤销语义；
4. legacy Redis session/auth-code 的一次性消费、PKCE 和删除 fence。

只有完成 identity import ledger、provider link replay、兼容 auth-code 表/lease、Firebase credential bridge 和客户端 conformance test 后，才允许把这 4 条路由的 owner 改为 Auth Worker。Better Auth social sign-in 本身不能作为证明。

## External App OAuth：MCP OAuth 不是同一个 authority

Legacy 实现位于 [`backend/routers/oauth.py`](../../backend/routers/oauth.py)：

- `GET /v1/oauth/authorize` 读取 Firestore app、capabilities、external integration 和 Firebase config，生成 CSRF cookie，并返回 consent HTML。
- `POST /v1/oauth/token` 校验 Firebase ID token 与 CSRF cookie，读取 app 的 private/tester/paid/setup 状态，必要时 enable app/increment installs，最后返回 `{uid, redirect_url, state}`。

现有 D1 `cf_app_catalog` 和 `/v1/mcp/oauth/*` 只覆盖 Cloudflare app projection 与 MCP server 的 OAuth grants。它们没有覆盖外部 App 的 `app_home_url`、Firebase token verifier、setup-completed callback、paid-app/install CAS、CSRF transaction 和 account deletion sweep。因此不能把 `/v1/mcp/authorize` 或 Better Auth session 直接挂到 `/v1/oauth/*`。

切换前必须新增并回放：

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

Workers 的 `fetch` 可以承载 Twilio REST 请求，但这不自动提供迁移。正向 owner 至少需要 `cf_phone_numbers`、pending verification 的唯一约束/TTL、account-generation/deletion fence、Twilio secret lifecycle、原子 quota reservation、webhook timestamp/replay protection、Queue retry/terminal state 和 token response tests。缺少其中任一项时，不能把 Twilio API 代理当成 phone migration。

## Wrapped：必须先定义结果 authority 和可重放 job

Legacy 实现位于 [`backend/routers/wrapped.py`](../../backend/routers/wrapped.py) 以及 `backend/utils/wrapped_analysis.py`：读取 Firestore `users/{uid}/wrapped/{year}`，生成时聚合 conversations/action items，调用 `wrapped_analysis`，通过本机 `llm_executor` 和通知路径异步落结果。

当前 Cloudflare 没有 Wrapped-specific result/job 表、历史 conversation/action-item replay contract、结构化 provider schema、Queue lease/retry 或通知 authority。可以调用 Workers AI/外部 LLM，但必须先定义输入上限、year 计算范围、provider JSON schema/version、partial/failure 状态、幂等键、删除 fence 和旧客户端 poll/response shape；否则空结果、同步 503 或“已完成但没有 recap”都不是 parity。

## Chat compatibility：现有新 API 不能替换旧桌面协议

Legacy 入口为：

- `POST /v1/chat/materialize-prompts`
- `POST /v2/chat/materialize-prompts`
- `POST /v2/chat/completions`

这些入口仍依赖旧 Firebase 身份、Firestore chat/materialization continuity，以及桌面端 provider、BYOK、Redis quota、tools、流式 SSE/usage/error 语义。Cloudflare 的 D1 `cf_chat_sessions`、`/v2/chat-sessions` 与无状态 `/v2/chat/generate-reply` 是新 contract：后者不创建 chat session/message，也不提供旧 completions 的流式 wire parity。

要迁移必须先完成 session/message reader 与历史回填，再锁定 provider selection/BYOK/quota/tool invocation/SSE schema 的 conformance fixtures；materialization 还需明确 D1 prompt authority、proactive intent 和跨设备幂等。仅把请求转发给 API AI 或 Workers AI 会丢失至少一个上述语义，继续保持 legacy owner。

## Persona / MCP mutation：已有 manifest refresh 不是 legacy refresh

`POST /v1/apps/{app_id}/mcp/refresh` 的 legacy 实现位于 [`backend/routers/apps.py`](../../backend/routers/apps.py)：它读取 Firestore `external_integration.mcp_server_url` 和 OAuth token，必要时刷新 token，再向 MCP server 做 authenticated tool discovery，并更新 `chat_tools` 和 public app cache。

Cloudflare 当前 `POST /v1/apps/{app_id}/refresh-manifest` 只针对 D1 中的 `chat_tools_manifest_url` 做 bounded HTTPS JSON fetch；Jobs app mutation 明确拒绝 `mcp_server_url` / `mcp_oauth_tokens` 写入，避免把 provider secrets 当普通 catalog JSON 存储。两个 endpoint 的输入、认证、token refresh、discovery、错误和返回 shape 不同，不能只做路径 alias。要迁移需要专门的加密/provider-token authority、MCP discovery adapter、CAS/cache invalidation、secret deletion 和 OAuth callback 回放。

同理，`POST /v1/apps/mcp`、`GET /v1/apps/mcp/callback` 和 `POST /v1/apps/migrate-owner` 仍分别依赖 dynamic client registration/PKCE、Firestore app creation/cache 与 Firebase anonymous identity proof，当前没有可证明的 Cloudflare parity。

## 可执行的迁移准入证据

任何一个阻塞组要切换 owner，提交必须同时包含以下证据：

1. route manifest 的 owner 变更与唯一 canonical handler；
2. D1/DO schema、account-generation/deletion fence、历史回放报告；
3. core success/error、跨 uid、重放/幂等、provider failure、retry/terminal、残留清理测试；
4. 旧客户端或旧 provider wire fixture（包括 redirect/HTML、form、SSE 或 token response，按路由适用）；
5. staging live probe：先证明新 owner 命中，再证明旧 backend 未被调用，最后完成真实正向/失败/删号探针。

在这些证据出现之前，`AUTH_OAUTH_STAGING_FAIL_CLOSED`、`PHONE_TWILIO_STAGING_FAIL_CLOSED`、`WRAPPED_STAGING_FAIL_CLOSED`、`CHAT_COMPAT_STAGING_FAIL_CLOSED` 和其它现有保护开关应继续保留；它们是泄露/误写防线，不计作迁移进度。
