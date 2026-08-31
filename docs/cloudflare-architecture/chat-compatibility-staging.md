# Cloudflare chat compatibility staging contract

截至 2026-08-31，Cloudflare 提供了一个显式的前置入口：
`POST /v2/cf/chat/completions`。它不改变已发布的三个 legacy 路由 owner：

- `POST /v1/chat/materialize-prompts`
- `POST /v2/chat/materialize-prompts`
- `POST /v2/chat/completions`

## 已闭合的前置能力

`/v2/cf/chat/completions` 使用 Better Auth 的 Edge assertion，并在 API AI
Python Worker 内完成：

- D1 `cf_chat_sessions` / `cf_chat_messages` 的 owner-scoped session continuity；
- D1 `cf_chat_quota_events` 的预留与 provider usage 结算；
- Workers AI 文本 provider（`workers-ai` / `cloudflare-workers-ai`）；
- 可选的、经过 Edge enrollment 校验的 `openai-byok` 非流式请求；
- deterministic message IDs、重复请求读取已持久化的 assistant response；
- OpenAI JSON response，以及 buffered `data:` SSE + `[DONE]` framing；
- account-deletion D1 triggers 继承的写入 fence。

请求体是严格的 text-only contract：`messages`、`model`、`stream`、
`max_tokens`/`max_completion_tokens`、`temperature` 和可选 `session_id`。
必须通过 `Idempotency-Key` 或 Edge request ID 形成稳定重试身份；未知字段、
tool calls、image blocks、server-side web search、旧模型 alias 和不匹配的
token 参数会在 provider/D1 写入前拒绝。

## 为什么旧入口仍未切换

旧 `/v2/chat/completions` 的请求者依赖 Anthropic model alias、client/server
tool calls、web-search policy、BYOK/Redis quota、pause-turn continuation，
以及专用的 Anthropic→OpenAI-compatible response/SSE usage 语义。两个
`materialize-prompts` 入口还依赖 Firestore proactive-intent continuity；
当前 Cloudflare D1 chat projection 不能证明这些状态是同一 authority。

因此 `/v2/cf/chat/completions` 是客户端迁移和 wire-contract 验证入口，不是
legacy alias，也不应把受限文本实现计入三个旧路由的迁移完成数。下一步只有在
工具、provider/quota、完整 SSE 和 materialization D1 authority 均完成回放验证后，
才可评估切换旧路由 owner。

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

因此 0113/0114 只扩大了可验证的 Cloudflare 前置，不改变三条 legacy 路由的
owner，也不构成 `/v2/chat/completions` 的切换证据。

## 逐路由闭合审计（2026-08-31）

本轮对三个 legacy 入口做了 bounded audit。结论是没有一个可以独立切 owner 的
最小闭合组；`/v2/cf/chat/completions` 是唯一已经形成正向 Cloudflare contract
的迁移 seam，但它故意不承诺旧客户端兼容。

| 入口 | 已有 Cloudflare 能力 | 仍缺的 authority / wire contract | 决策 |
| --- | --- | --- | --- |
| `POST /v1/chat/materialize-prompts` | `chat-first` block validation 和 D1 deferral 可校验部分 uid/generation/entity fence | legacy 读取并确认 Firestore proactive-intent、cold-start/daily-opener/deferral release、一次性 materialization receipt 和 account-wide delivery state；当前没有 D1 intent reader/ack 表 | 保持 legacy owner；不把 block validation 或 deferral 当作 materialization 迁移 |
| `POST /v2/chat/materialize-prompts` | 同上；可验证的 block 类型可以映射到 D1 canonical entity | v2 还要保留完整 intent/block union（包括 `conversationLink`）、foreground/initial-page admission、receipt 幂等和历史 Firestore 回放；当前 `chat_first_routes.py` 没有 prompt materialization handler | 保持 legacy owner；不能把 `/v1/chat-first/blocks/validate` 路径 alias 过来 |
| `POST /v2/chat/completions` | `/v2/cf/chat/completions` 有 D1 session/history/quota、Workers AI 或已验证 OpenAI BYOK、持久化和 buffered SSE | 旧 `desktop_chat.py` 仍有 Anthropic managed/gateway lane、Anthropic BYOK、Redis burst/daily quota、web-search policy、tool calls、pause-turn continuation、旧 SSE/usage/error 语义；这些不是当前 text-only contract 的可忽略字段 | 保持 legacy owner；要求客户端迁移到显式 `/v2/cf` contract 后再评估 alias |

审计对应的实现边界如下：旧 materialization 的请求/响应模型和 Firestore 读取/确认
在 [`backend/routers/chat_first.py`](../../backend/routers/chat_first.py) 与
[`backend/database/chat_first_intents.py`](../../backend/database/chat_first_intents.py)，
Cloudflare 目前只有 [`chat_first_routes.py`](../../deploy/cloudflare/python/api-core/src/chat_first_routes.py)
的 block validation/deferral projection；旧 completion 的 provider、工具和 stream
分支在 [`backend/routers/desktop_chat.py`](../../backend/routers/desktop_chat.py)，
而新 text-only seam 在 [`chat_generation_routes.py`](../../deploy/cloudflare/python/api-ai/src/chat_generation_routes.py)。
因此只把 Edge owner 或 manifest 改成 API AI 会产生一个能返回 200、但会丢失连续性
或工具/usage 语义的兼容假象。

### owner 切换前必须具备的 replay fixtures

后续若要切换任一 legacy owner，提交中必须附带可重复执行的 fixture，而不是只测
HTTP 状态码：

1. **Materialization authority fixture**：每个 proactive source（daily opener、capture arrival、deferral re-raise、agent judgment、cold-start rich/sparse）的 Firestore 导出行，包含完整 block union、account generation、foreground/initial-page 窗口、重复/过期/跨 uid receipt；回放到 D1 后必须证明 ready→delivered 是单次原子转移。
2. **Desktop completion fixture**：managed Anthropic/gateway、直接 Anthropic BYOK、不同 model alias、web search、tool call、pause-turn continuation、非流式和流式成功，以及 401/402/429/502/usage 缺失等 provider/quota 错误；fixture 要保留旧 SSE data frame、usage 和 error envelope，而不是只比较 assistant 文本。
3. **Session/backfill fixture**：Firebase UID 到 Better Auth uid 的映射、历史 Firestore chat/session/message、文件引用和 app/persona scope；重复 replay、跨 uid、旧 session id、缺失/损坏历史行都必须得到确定结果。
4. **Mutation/deletion fixture**：quota reservation/settlement、provider failure rollback、重复 Idempotency-Key、并发同一 session，以及 account deletion fence；删除后 D1 chat/session/quota/materialization/provider receipt 残留必须为零或有明确 tombstone。

在上述 fixture 具备前，`CHAT_COMPAT_STAGING_FAIL_CLOSED=true` 是必要的数据泄露防线。
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
