# Cloudflare chat compatibility staging contract

截至 2026-09-01，Cloudflare staging 已将下面三个 exact 入口交给 Jobs
Worker；生产仍需完成 provider/backfill 回放后再切换：

- `POST /v1/chat/materialize-prompts`
- `POST /v2/chat/materialize-prompts`
- `POST /v2/chat/completions`

Edge 通过 Better Auth assertion、产品 cutover fence 和
`chat:send_message` Durable Object rate limit 转发到 Jobs。Jobs 仅在
`CHAT_COMPATIBILITY_CLOUDFLARE_ENABLED=true` 的部署中注册 owner；缺少
provider secret 时明确返回 `503 provider_not_configured`，不会回落 legacy。

## 已闭合的前置能力

Exact `/v2/chat/completions` 使用同一 Better Auth assertion，并在 Jobs Worker
内完成：

- D1 `cf_chat_sessions` / `cf_chat_messages` 的 owner-scoped session continuity；
- D1 `cf_chat_quota_events` 的预留与 provider usage 结算；
- Workers AI 文本 provider（`workers-ai` / `cloudflare-workers-ai`）；
- 可选的、经过 Edge enrollment 校验的 `openai-byok` 请求（validated
  `x-byok-openai` header 不落 D1）；
- deterministic message IDs、重复请求读取已持久化的 assistant response；
- OpenAI JSON response，以及 buffered `data:` SSE + `[DONE]` framing；
- account-deletion D1 triggers 继承的写入 fence。

请求体是严格的 text-only contract：`messages`、`model`、`stream`、
`max_tokens`/`max_completion_tokens`、`temperature` 和可选 `session_id`。
必须通过 `Idempotency-Key` 或 Edge request ID 形成稳定重试身份；未知字段、
tool calls、image blocks、server-side web search、旧模型 alias 和不匹配的
token 参数会在 provider/D1 写入前拒绝。

## MCP tool projection boundary

Cloudflare 现在允许该显式 seam 接收 `app_id` + 非空 `tools` 作为 MCP
preflight。API-AI 会按 Better Auth uid 查询 D1 中已安装、`authorized` 且
discovery=`ready` 的 projection（与 API Core 的
`GET /v2/cf/apps/mcp/tools` 使用同一组 owner/status 条件），只返回安全的
tool name 列表并以 `409 mcp_tool_execution_not_migrated` 明确拒绝执行；缺少
projection 返回 `404`，损坏或不可读的 projection 返回 `503`。该分支发生在
quota reservation、Workers AI/OpenAI BYOK provider 调用和 chat message 写入
之前，因此不会产生计费或伪造 assistant 回复。

这只是把已安装 MCP tool authority 接入 chat 的可验证拒绝边界，不是 MCP
runtime。完整 tool schema、上游 credential/session、JSON-RPC `tools/call`、
结果回写、重试和旧桌面 tool/SSE parity 仍未完成；这些字段在 exact
completion route 仍会得到 `409 unsupported_chat_feature`。

## 为什么生产入口仍未切换

旧 `/v2/chat/completions` 的请求者依赖 Anthropic model alias、client/server
tool calls、web-search policy、BYOK/Redis quota、pause-turn continuation，
以及专用的 Anthropic→OpenAI-compatible response/SSE usage 语义。两个
`materialize-prompts` 入口还依赖 Firestore proactive-intent continuity；
当前 Cloudflare D1 chat projection 不能证明这些状态是同一 authority。

`/v2/cf/chat/completions` 仍保留给显式客户端迁移；exact route 现在使用同一
底层实现，但只承诺 text-only subset。完整旧 Anthropic/gateway、工具、web
search、pause-turn、旧 Firestore 历史和 byte-for-byte SSE/usage parity 仍未
完成，因而这次是 staging owner 闭合，不是 production compatibility parity。

## 0113/0114 provider recheck

当前 workspace 还包含显式 opt-in 的 OpenAI Assistants continuity adapter
（`0113_chat_assistant_provider.sql`）。它把 D1 session 映射到 provider
thread/run，并由 Jobs Queue 轮询；入口是
`/v2/cf/chat-sessions/:sessionId/assistant-runs`，admission 返回 `202`，
随后通过 GET 读取 terminal result。它不是 `/v2/chat/completions` 的实现：

- 旧 completion 的 managed Anthropic/gateway、Anthropic BYOK、model alias、
  web-search、client/server tool、pause-turn continuation 和 OpenAI-compatible
  SSE/usage/error contract 仍未由该 adapter 提供；
- Assistants adapter 的 provider session/run projection 不写
  `cf_chat_messages`，也没有把旧 Redis burst/daily quota 和 Firestore chat
  history 回放接入同一事务；
- text-only `/v2/cf/chat/completions` 和 Assistants adapter 的响应时序、provider
  及文件/tool 语义都不同，不能通过 Edge alias 伪装成旧客户端兼容。

因此 0113/0114 只扩大了可验证的 Cloudflare 前置；本次 Jobs owner 仅适用于
staging，不构成 `/v2/chat/completions` 的 production parity 或历史切换证据。

## 逐路由闭合审计（2026-09-01）

本轮对三个入口完成了 bounded Cloudflare owner slice。D1/Workers AI/OpenAI
REST 的可用 subset 已切到 staging；这不是完整 legacy parity，且 production
仍需 provider/backfill 回放证据。

| 入口 | 已有 Cloudflare 能力 | 仍缺的 authority / wire contract | 决策 |
| --- | --- | --- | --- |
| `POST /v1/chat/materialize-prompts` | D1 `cf_chat_first_intents`、canonical goals/tasks、foreground/initial-page admission、receipt CAS、deferral release、daily opener 和 deletion fence | 历史 Firestore intent replay、完整 cold-start source/eligibility 及旧客户端 fixture 尚未 backfill；v1 仍过滤 `conversationLink` | staging Jobs owner；生产切换前需 replay fixture |
| `POST /v2/chat/materialize-prompts` | 同上，并保留 v2 完整 block union（包括 `conversationLink`） | 历史 Firestore intent replay、旧客户端 wire conformance 和完整 entity availability fixture 尚未 backfill | staging Jobs owner；生产切换前需 replay fixture |
| `POST /v2/chat/completions` | Jobs D1 session/history/quota、Workers AI 或已验证 OpenAI/BYOK REST、持久化、OpenAI JSON 和 buffered SSE | 旧 `desktop_chat.py` 的 Anthropic managed/gateway、web-search、tool calls、pause-turn、历史 Firestore、旧 SSE/usage/error byte parity 未闭合 | staging Jobs owner；生产仅承诺 text-only subset |

审计对应的实现边界如下：旧 materialization 的请求/响应模型和 Firestore 读取/确认
在 [`backend/routers/chat_first.py`](../../backend/routers/chat_first.py) 与
[`backend/database/chat_first_intents.py`](../../backend/database/chat_first_intents.py)，
Cloudflare 目前只有 [`chat_first_routes.py`](../../deploy/cloudflare/python/api-core/src/chat_first_routes.py)
的 block validation/deferral projection；旧 completion 的 provider、工具和 stream
分支在 [`backend/routers/desktop_chat.py`](../../backend/routers/desktop_chat.py)，
而新 text-only seam 在 [`chat_generation_routes.py`](../../deploy/cloudflare/python/api-ai/src/chat_generation_routes.py)。
实现位于 [`chat-compatibility.ts`](../../deploy/cloudflare/workers/jobs/chat-compatibility.ts)，
Edge 只转发经过 Better Auth、cutover 和 rate-limit 检查的请求；因此 route owner
不再是“只把 Edge manifest 改成 API AI”，而是有 D1 写入和 provider boundary 的
Jobs implementation。它仍拒绝不支持的工具、web-search、结构化输出和历史 provider
alias，不伪造成功。

### owner 切换前必须具备的 replay fixtures

后续若要切换任一 legacy owner，提交中必须附带可重复执行的 fixture，而不是只测
HTTP 状态码：

1. **Materialization authority fixture**：每个 proactive source（daily opener、capture arrival、deferral re-raise、agent judgment、cold-start rich/sparse）的 Firestore 导出行，包含完整 block union、account generation、foreground/initial-page 窗口、重复/过期/跨 uid receipt；回放到 D1 后必须证明 ready→delivered 是单次原子转移。
2. **Desktop completion fixture**：managed Anthropic/gateway、直接 Anthropic BYOK、不同 model alias、web search、tool call、pause-turn continuation、非流式和流式成功，以及 401/402/429/502/usage 缺失等 provider/quota 错误；fixture 要保留旧 SSE data frame、usage 和 error envelope，而不是只比较 assistant 文本。
3. **Session/backfill fixture**：Firebase UID 到 Better Auth uid 的映射、历史 Firestore chat/session/message、文件引用和 app/persona scope；重复 replay、跨 uid、旧 session id、缺失/损坏历史行都必须得到确定结果。
4. **Mutation/deletion fixture**：quota reservation/settlement、provider failure rollback、重复 Idempotency-Key、并发同一 session，以及 account deletion fence；删除后 D1 chat/session/quota/materialization/provider receipt 残留必须为零或有明确 tombstone。

在上述 fixture 具备前，生产必须保持关闭 `CHAT_COMPATIBILITY_CLOUDFLARE_ENABLED`
或仅以可删除 staging 账号运行；provider 不可用时 Jobs 固定返回 503，不回落
legacy。旧的 `CHAT_COMPAT_STAGING_FAIL_CLOSED=true` 仍可用于验证 legacy rollback
路径。
Edge 回归测试还必须确认带有 opaque cookie、Bearer、auth-context、BYOK header 和
prompt 的请求不会读取 body 或调用 legacy backend。

## 验证

API AI focused suite 覆盖：D1 session/history、quota settlement、重复请求、
buffered SSE、认证和旧 tool shape fail-closed。运行：

```bash
deploy/cloudflare/python/api-ai/.venv/bin/pytest -q \
  deploy/cloudflare/python/api-ai/tests/test_chat_generation_routes.py
```

Edge route 与 manifest 也必须通过 `npm run typecheck` 和
`npm run validate:manifest`；当前工作区其他并行 phone/wrapped 变更可能使全局
typecheck 在其自身文件上失败，这不表示本 contract 的 Python focused tests 失败。
