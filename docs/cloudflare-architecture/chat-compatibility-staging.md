# Cloudflare chat compatibility staging contract

截至 2026-09-01，Cloudflare staging 已将下面三个 exact 入口交给 Jobs
Worker；本期按空数据新部署验收，不要求历史回填或旧客户端 wire parity：

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

## 旧协议边界（本期不阻塞）

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

因此 0113/0114 只扩大了可验证的 Cloudflare provider 前置；本期不把旧
Anthropic/gateway、工具、旧 Firestore 历史或 byte-for-byte SSE parity 作为
新部署阻塞项。若未来要继续支持旧桌面客户端，再单独开启兼容迁移窗口。

## 逐路由闭合审计（2026-09-01）

本轮对三个入口完成了 bounded Cloudflare owner slice。D1/Workers AI/OpenAI
REST 的可用 subset 已切到 staging；新部署只需配置实际使用的 provider 并做
authenticated smoke，不要求历史回填或 legacy parity。

| 入口 | 已有 Cloudflare 能力 | 仍缺的 authority / wire contract | 决策 |
| --- | --- | --- | --- |
| `POST /v1/chat/materialize-prompts` | D1 `cf_chat_first_intents`、canonical goals/tasks、foreground/initial-page admission、receipt CAS、deferral release、daily opener 和 deletion fence | v1 仍过滤 `conversationLink`；旧 Firestore replay/旧客户端 fixture 不在本期 | staging Jobs owner；启用前做 provider/Queue smoke |
| `POST /v2/chat/materialize-prompts` | 同上，并保留 v2 完整 block union（包括 `conversationLink`） | 旧 Firestore replay/旧客户端 wire fixture 不在本期 | staging Jobs owner；启用前做 provider/Queue smoke |
| `POST /v2/chat/completions` | Jobs D1 session/history/quota、Workers AI 或已验证 OpenAI/BYOK REST、持久化、OpenAI JSON 和 buffered SSE | 旧 provider/tool/pause-turn parity 不在本期 | staging Jobs owner；新客户端仅使用 text/app/context/attachment subset |

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

### 本期启用前必须具备的 fixtures

本期启用任一 Cloudflare owner，提交中必须附带可重复执行的 fixture，而不是只测
HTTP 状态码；旧协议 fixture 只在未来兼容窗口需要：

1. **Materialization authority fixture**：新客户端的 daily opener、deferral 和 cold-start source，覆盖 block union、foreground/initial-page 窗口、重复/过期/跨 uid receipt；必须证明 ready→delivered 是单次原子转移。
2. **Provider fixture**：Workers AI、已启用的 OpenAI/Gemini/Twilio/Calendar provider，覆盖成功和 401/402/429/502/usage 缺失等错误；新客户端只需验证当前 JSON/SSE/Queue contract。
3. **Session/app fixture**：Better Auth uid、D1 chat/session/message、文件引用和 app/persona scope；重复请求、跨 uid、缺失/损坏行都必须得到确定结果。
4. **Mutation/deletion fixture**：quota reservation/settlement、provider failure rollback、重复 Idempotency-Key、并发同一 session，以及 account deletion fence；删除后 D1 chat/session/quota/materialization/provider receipt 残留必须为零或有明确 tombstone。

provider secret 尚未配置时 Jobs 固定返回 503，不回落 legacy；旧的
`CHAT_COMPAT_STAGING_FAIL_CLOSED=true` 仅用于验证显式 rollback 路径。
Edge 回归测试还必须确认带有 opaque cookie、Bearer、auth-context、BYOK header 和
prompt 的请求不会读取 body 或调用 legacy backend。

## Web 原生 app/context 聊天

Web `/v2/messages` 的无附件文本请求现在可直接使用 API-AI 的 Workers AI
authority，并支持 `?app_id=` 与 bounded `context`：

- app 必须存在且未禁用；公开 app、owner 和 tester 按 D1 catalog projection
  读取，找不到或不可用时在 quota/provider 之前返回 `404 app_not_found`；
- session、history、quota message 与 `cf_chat_messages.app_id` 都按
  `(uid, app_id)` 隔离，app 的 `chat_prompt`/`persona_prompt` 作为系统身份；
- `context` 只允许 `type/id/title/summary`，各字段有长度上限，以
  `PAGE CONTEXT (untrusted reference data)` 注入 prompt，不写入消息 authority；
- `file_ids` 走单独的 Jobs Assistant bridge：先返回 `202` + `Location`，Web
  客户端轮询 run 资源，并在 terminal result 后适配回同一 `data:/done:` 回调；
  该路径要求 `OPENAI_API_KEY`、`OPENAI_ASSISTANT_ID` 和
  `CHAT_ASSISTANT_PROVIDER_STAGING_ENABLED=true`，缺少 provider 时稳定返回
  `503`，不会回落 legacy。Workers AI 仍不直接读取 R2 文件。

这使当前 Web app/persona 和页面上下文路径成为 Cloudflare-native 的新客户端
能力；不承诺旧 Firebase/Firestore 历史回放或旧 provider/tool wire parity。

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
