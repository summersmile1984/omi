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
