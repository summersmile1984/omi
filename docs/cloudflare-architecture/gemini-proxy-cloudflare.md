# Gemini desktop proxy on Cloudflare

截至 2026-08-31，`POST /v1/proxy/gemini/{path}` 和
`POST /v1/proxy/gemini-stream/{path}` 仍是 `legacy-owned`。本说明把
“能不能在 Workers 上发 Gemini 请求”和“能不能替换旧桌面代理”分开：前者
可行，后者目前没有足够的 authority 和 wire-contract 证据，不能仅把路径
改成 API-AI 或 Workers AI。

## 结论

Cloudflare Worker 可以通过 `fetch` 调用 Gemini REST API；Gemini 官方 REST
接口提供 `generateContent` 和 `streamGenerateContent?alt=sse`，流式响应是
多个 `GenerateContentResponse` 事件，而不是 OpenAI Chat Completions 事件。
Better Auth 也原生支持把 D1 作为数据库，因此新建的 Cloudflare-only
Gemini surface 可以使用 Better Auth session、Edge signed context 和 D1。

这仍不足以接管旧入口，原因是当前没有同时闭合：

1. 旧 Firebase principal 到 Better Auth user 的可回滚 identity bridge。Auth
   Worker 的 `/internal/verify` 当前证明的是 Better Auth session/JWT，不能把
   旧 Firebase bearer token 当作已迁移身份。
2. 公司付费流量的 Vertex ADC / Provisioned Throughput 路由。AI Studio
   `GEMINI_API_KEY` 不能替代现有 Vertex PT、地域和 `trafficType` 语义；把
   `/v1/ai/*` 的固定 OpenAI-compatible proxy 当 Gemini adapter 也会改变协议。
3. 旧 Redis burst（30/60 秒）和 daily hard limit（1500/24 小时）的原子
   admission、Pro 降级、失败/重放计费及 cost accounting。现在的 guarded
   adapter 已有 Edge DO 30/60 policy 和 D1 daily ledger，但这不等于旧 Redis
   的完整语义或生产配额迁移。
4. `x-byok-gemini` 的请求级密钥 authority、未入日志的 provider header、旧
   客户端对模型/action 白名单和错误头的依赖。现在已有仅供 Cloudflare
   adapter 使用的 AI Studio provider route；它仍不是 Firebase/Vertex 兼容
   的公开 owner。
5. 非流式 JSON、Gemini SSE 分块、`usageMetadata`、Vertex `trafficType`、
   `X-Omi-*` 错误头和 timeout/retry 语义的旧客户端 conformance fixture。

因此 staging 继续由 `GEMINI_PROXY_STAGING_FAIL_CLOSED=true` 保护，且
`GEMINI_PROXY_CLOUDFLARE_ENABLED=false`：两个
入口返回 `503 gemini_proxy_unavailable`、`cache-control: no-store`，不读取
body、不把 prompt、cookie、Firebase token 或 BYOK key 发给 legacy。该保护
不是 owner migration，也不减少 manifest 中的两条 `legacy-owned`。

## 已落地但未切 owner 的最小 Cloudflare 设计

### Provider adapter

`deploy/cloudflare/python/api-ai/src/gemini_proxy_routes.py` 新增了唯一的
Gemini adapter（API-AI Worker；Edge 只负责认证、BYOK 校验和 signed context），
而不是扩大通用 `/v1/ai/{path}`。它默认不由 Edge public route 调用，必须显式
打开 `GEMINI_PROXY_CLOUDFLARE_ENABLED` 才能做隔离验证：

- 只接受 `models/{model}:{action}`，沿用旧 allowlist；未知模型/action 在
  provider dispatch 前返回 403。
- 当前实现只允许显式配置的 AI Studio `GEMINI_API_KEY`，仅作为 adapter
  fixture/staging provider；server-paid Vertex service identity、regional/PT
  路由没有实现，provider 选择不是 `ai_studio` 时 fail closed。
- BYOK 只在 Edge 通过 `cf_user_byok_enrollments` fingerprint 校验后进入
  request-local context。发送到 AI Studio 时优先使用 `x-goog-api-key`，不把
  raw key 放 query、日志、D1 或内部 assertion；请求结束即丢弃。
- adapter 有界读取 JSON（不超过旧 5 MiB 上限），保留 `contents`、
  `generationConfig`、inline media 的上限和旧的 system-role 规范化；响应
  只复制允许的 content type、SSE 和 `X-Omi-*` headers。
- 对 `generateContent`/`streamGenerateContent`，Worker 复现桌面端的单候选
  约束，并将 server-paid 请求的 `maxOutputTokens` 限制为 2048；BYOK 请求
  保留历史 8192 上限。缺失预算时也会补上对应上限，避免合法但未声明的
  provider 配置绕过付费成本边界；embedding action 不注入文本输出预算。
- provider 状态必须映射为固定错误 envelope：400/403 为不可重试请求或
  credential 错误，429 带 `Retry-After` 且可重试，408/504 为 timeout，5xx
  为 502 provider unavailable；错误 body 不透传 prompt、key 或上游原文。

### Quota 与 usage authority

guarded adapter 的 burst 由现有 Edge Durable Object 串行执行，保持 30 requests /
60 seconds；daily hard limit 和 usage receipt 由 `0114_gemini_proxy.sql` 的
D1 transaction 维护，并已接入 account-deletion residual inventory。当前 schema
如下：

```sql
CREATE TABLE cf_gemini_quota_windows (
  uid TEXT NOT NULL,
  window_kind TEXT NOT NULL CHECK (window_kind IN ('daily')),
  window_start INTEGER NOT NULL,
  request_count INTEGER NOT NULL CHECK (request_count >= 0),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, window_kind, window_start)
);

CREATE TABLE cf_gemini_usage_receipts (
  request_id TEXT PRIMARY KEY,
  uid TEXT NOT NULL,
  model TEXT NOT NULL,
  action TEXT NOT NULL,
  credential_source TEXT NOT NULL CHECK (credential_source IN ('byok', 'server')),
  provider TEXT NOT NULL CHECK (provider = 'ai_studio'),
  status TEXT NOT NULL CHECK (status IN ('reserved', 'success', 'rejected', 'failed')),
  prompt_tokens INTEGER,
  output_tokens INTEGER,
  total_tokens INTEGER,
  cached_input_tokens INTEGER,
  reasoning_tokens INTEGER,
  traffic_type TEXT,
  estimated_cost_micros INTEGER,
  created_at INTEGER NOT NULL,
  completed_at INTEGER
);
```

The reservation key must be the authenticated uid plus a bounded request id, not a
caller-supplied uid. A retry of the same request id must not increment the daily
counter or emit a second cost receipt. Provider responses are the only source for
token counts; an absent `usageMetadata` is an explicit `unknown` accounting state,
not zero. Cost rates need a versioned, reviewed rate-card source before server-paid
traffic can be enabled.

### Deletion and migration authority

Both tables need uid-scoped deletion-intent/tombstone protection and residual
queries. A request admitted after an account deletion fence must be rejected before
provider dispatch. Historical Redis quota and provider usage cannot be inferred
from D1; a migration report must either replay a bounded export or explicitly mark
the old account as not eligible for the new owner.

## Required fixture and test contract

The adapter test set adds fixtures under a Gemini-specific test module and exercises
production seams, not source-string assertions. It currently covers direct JSON
provider dispatch/usage, missing-secret fail-closed behavior, D1 daily reservation,
stream-route action validation, credential-query rejection, and query forwarding.
The following are still required before any owner change:

- **Auth:** Better Auth session success; missing/expired session; an unmigrated
  Firebase bearer principal; wrong signed audience; and account-generation/deletion
  fence. The unmigrated Firebase case must remain fail-closed until the identity
  bridge is proven.
- **Routing:** every allowed model/action; alias `gemini-3-flash-preview`;
  disallowed model/action; missing Vertex identity/project; BYOK enrolled,
  expired, mismatched and raw-key-not-forwarded cases; server-paid requests never
  using AI Studio.
- **Request limits:** malformed JSON, non-object body, 5 MiB boundary, too many
  contents/parts/inline media, forbidden credential query parameters, and bounded
  response body.
- **Wire fixtures:** exact single JSON response with `usageMetadata`; SSE with
  arbitrary TCP chunk boundaries, CRLF/LF, multiple `data:` lines and a terminal
  usage event; provider 400/403/404/408/429/5xx and transport timeout mapped to the
  documented status, headers and retryability. Gemini SSE must stay Gemini-shaped;
  do not normalize it to OpenAI `[DONE]` events without a separate client contract.
- **Quota/accounting:** 31st burst request, daily 1500th/1501st request,
  concurrent reservations for one uid, same request-id replay, provider rejection
  and retry, Pro soft-limit demotion, missing usage metadata, and cost-card
  versioning. Assert no raw key, prompt or upstream error body appears in logs or
  receipts.
- **Deletion:** admission racing account deletion, residual receipt/window scan,
  and a second request after the tombstone. Assert no provider call occurs after
  the fence.
- **Live staging gate:** with a disposable completed Cloudflare account and a
  provider fixture, prove authenticated positive JSON and SSE responses, quota
  receipts, and deletion cleanup; then prove the legacy backend was not called.
  Without Vertex/BYOK credentials and these fixtures, only the current anonymous
  401 boundary and staging fail-closed 503 probe may be reported.

The current regression test in
[`deploy/cloudflare/tests/edge.test.ts`](../../deploy/cloudflare/tests/edge.test.ts)
already proves that both staging paths return the fail-closed envelope and that a
request containing an opaque BYOK key/prompt does not invoke legacy `fetch`. It is
not evidence of a positive provider migration.

## Cutover gate

Only after the identity bridge, provider identity, DO/D1 quota transaction,
versioned usage accounting, fixtures, deletion fence, and authenticated staging
probe all pass should the manifest owner move. Until then the correct Cloudflare
posture is to keep the two routes `legacy-owned` and fail closed in isolated
staging.

References: [Gemini REST generateContent and streamGenerateContent](https://ai.google.dev/gemini-api/docs/generate-content/text-generation),
[Gemini GenerateContent API](https://ai.google.dev/api/generate-content), and
[Better Auth database/D1 documentation](https://better-auth.com/docs/concepts/database).
