# Gemini desktop proxy on Cloudflare

截至 2026-09-01，`POST /v1/proxy/gemini/{path}` 和
`POST /v1/proxy/gemini-stream/{path}` 已登记为 Cloudflare
`staging-owned`：Edge 负责 Better Auth/Firebase bridge 身份验证、BYOK
指纹校验和 30/60 burst，API-AI Worker 负责 Gemini JSON/SSE provider
adapter、D1 daily ledger 与 usage accounting。生产 owner promotion 仍需
完成旧客户端兼容和真实 Vertex/provider 验证；不能把 staging owner 当成
生产 parity。该 proxy 是可选的桌面兼容面，不是 Workers AI 新部署的生成式
AI 依赖；不启用时不需要任何 Gemini secret。

## 结论

Cloudflare Worker 可以通过 `fetch` 调用 Gemini REST API；Gemini 官方 REST
接口提供 `generateContent` 和 `streamGenerateContent?alt=sse`，流式响应是
多个 `GenerateContentResponse` 事件，而不是 OpenAI Chat Completions 事件。
Better Auth 也原生支持把 D1 作为数据库，因此新建的 Cloudflare-only
Gemini surface 可以使用 Better Auth session、Edge signed context 和 D1。

Cloudflare staging owner 仅在显式启用且配置 provider secret 的环境中发起
Gemini 请求；缺少 secret 时 API-AI 明确返回 `503`，不会回退到 legacy。新
客户端的 Workers AI 路径不读取这些 secret，也不依赖该 proxy。生产
兼容仍有以下未闭合项：

1. 旧 Firebase principal 到 Better Auth user 的可回滚 identity bridge。Auth
   Worker 的 `/internal/verify` 当前证明的是 Better Auth session/JWT，不能把
   旧 Firebase bearer token 当作已迁移身份。
2. 公司付费流量的 Vertex ADC / Provisioned Throughput 路由。AI Studio
   `GEMINI_API_KEY` 不能替代现有 Vertex PT、地域和 `trafficType` 语义；把
   `/v1/ai/*` 的固定 OpenAI-compatible proxy 当 Gemini adapter 也会改变协议。
3. 旧 Redis burst（30/60 秒）和 daily hard limit（1500/24 小时）的完整
   admission、Pro 降级、失败/重放计费及历史配额迁移。当前 owner 已有 Edge
   DO 30/60 policy 和 D1 daily ledger；历史 Redis 账本仍未回放。
4. `x-byok-gemini` 的请求级密钥 authority、未入日志的 provider header、旧
   客户端对模型/action 白名单和错误头的依赖。当前 API-AI adapter 已成为
   staging exact route 的 provider owner，但尚未宣称 Firebase/Vertex 生产
   兼容。
5. 非流式 JSON、Gemini SSE 分块、`usageMetadata`、Vertex `trafficType`、
   `X-Omi-*` 错误头和 timeout/retry 语义的旧客户端 conformance fixture。

staging 使用 `GEMINI_PROXY_CLOUDFLARE_ENABLED=true` 将 exact route 送入
Cloudflare owner；API-AI 的 `GEMINI_PROXY_ENABLED`、provider 选择和 provider
secret 仍独立控制实际 provider dispatch。关闭 Cloudflare switch 或缺少
secret 时返回 `503 gemini_proxy_unavailable`/provider-specific error，带
`cache-control: no-store`，不向 legacy 转发 prompt、cookie、Firebase token
或 BYOK key。生产环境仍需单独的 rollout 与回滚证据。

## 已落地的 Cloudflare staging owner

### Provider adapter

`deploy/cloudflare/python/api-ai/src/gemini_proxy_routes.py` 是唯一的
Gemini adapter（API-AI Worker；Edge 只负责认证、BYOK 校验、burst 和 signed
context），而不是扩大通用 `/v1/ai/{path}`。exact route 通过
`GEMINI_PROXY_CLOUDFLARE_ENABLED` 显式接入该 adapter：

- 只接受 `models/{model}:{action}`，沿用旧 allowlist；未知模型/action 在
  provider dispatch 前返回 403。
- AI Studio branch 只允许显式配置的 `GEMINI_API_KEY`，仅作为 adapter
  fixture/staging provider；server-paid Vertex service identity 走下方单独的
  opt-in seam，仍不实现旧 regional/PT 路由，且未知 provider 选择 fail closed。
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

### Vertex service-account seam（仅显式 provider opt-in）

API-AI 现在还提供一个可配置但尚未完成旧 PT/ADC parity 的 Vertex
service-account seam：
`GEMINI_PROXY_PROVIDER=vertex`（或 `vertex_ai`）时，Worker 从 secret
`GEMINI_VERTEX_SERVICE_ACCOUNT_JSON` 解析 project/client email/PKCS#8 key，
使用 Workers Web Crypto 的 `RSASSA-PKCS1-v1_5`/SHA-256 签署 OAuth 2.0 JWT
bearer assertion，再向 `oauth2.googleapis.com/token` 换取短期
`cloud-platform` access token。access token 只保留在 isolate 内的有界缓存，
不会写入 D1、日志或内部 assertion；私钥轮换会通过 key digest 使缓存失效。

Vertex endpoint 默认按 `GEMINI_VERTEX_LOCATION`（默认 `us-central1`）构造：
`https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:{action}`；
也可以用 `GEMINI_VERTEX_API_BASE_URL` 指定同一 Google `aiplatform.googleapis.com`
域名下的 `/v1` base。`GEMINI_VERTEX_PROJECT_ID`（若设置）必须与 service
account 的 `project_id` 一致。当前 seam 支持 `generateContent`、
`streamGenerateContent` 和单条 `embedContent`（转换成 Vertex `predict` wire）；
`batchEmbedContents`、请求级 BYOK 和缺失/非法 service identity 会 fail closed。
`0117_gemini_vertex_provider.sql` 扩展 usage receipt 的 provider check，并保留
已有 D1 receipt/quota/deletion-fence 数据。

部署前需将完整 service-account JSON 作为 API-AI Worker secret 写入，例如：
`wrangler secret put GEMINI_VERTEX_SERVICE_ACCOUNT_JSON --config deploy/cloudflare/python/api-ai/wrangler.jsonc`；
默认 provider 仍是 `ai_studio`，必须显式设置 `GEMINI_PROXY_PROVIDER=vertex_ai`
（或 `vertex`）后才会尝试 Vertex token exchange。

该 seam 证明的是 Workers→Google OAuth/Vertex 的可部署 provider boundary，
不是旧桌面代理的 Vertex PT/ADC 路由 parity：仍缺 PT `requestType`/overflow
ladder、Redis burst/Pro demotion、Firebase principal continuity、真实成本卡及
authenticated staging positive probe。因此 staging 仍保持
`GEMINI_PROXY_CLOUDFLARE_ENABLED=true`；没有 Vertex service identity 时，该
provider 会明确返回 503。production 仍不能把配置 seam 或单元测试当作 legacy
owner cutover 证据。

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
  provider TEXT NOT NULL CHECK (provider IN ('ai_studio', 'vertex_ai')),
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
The following are still required before production owner promotion:

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
  401 boundary and provider-missing 503 probe may be reported.

The current regression tests in
[`deploy/cloudflare/tests/edge.test.ts`](../../deploy/cloudflare/tests/edge.test.ts)
prove both the enabled exact-route forwarding boundary and the disabled legacy
fallback; the API-AI suite also proves missing-secret, unenrolled-BYOK, bounded
request, stream-overflow settlement, JSON/SSE provider dispatch, and D1 ledger
behavior. A unit test is not a substitute for a positive production-provider
probe.

## Cutover gate

Before production promotion, the identity bridge, provider identity, DO/D1 quota
transaction, versioned usage accounting, fixtures, deletion fence, and an
authenticated staging positive probe must all pass. Until then the correct posture
is Cloudflare staging ownership with explicit provider fail-closed behavior; any
environment without a configured provider remains a safe `503`, never an implicit
legacy fallback.

References: [Gemini REST generateContent and streamGenerateContent](https://ai.google.dev/gemini-api/docs/generate-content/text-generation),
[Gemini GenerateContent API](https://ai.google.dev/api/generate-content), and
[Better Auth database/D1 documentation](https://better-auth.com/docs/concepts/database).
