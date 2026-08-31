# Auth / OAuth Cloudflare migration gates

审计日期：2026-08-31

本文定义 staging owner 与后续生产切换所需的 authority、schema 和 fixture。
Better Auth 官方支持 Cloudflare Workers/Hono 与 D1；仓库中的
Auth Worker 已经使用这条组合承载 `/api/better-auth/*`。这里的阻塞点不是
Workers 不能运行 Better Auth，而是 Better Auth 的 session/OAuth wire 与 Omi
现有 Firebase 客户端协议不同。

## 当前 route decision

以下六条入口已经切到 Auth/Jobs staging owner；缺少 provider/identity
secrets 时由 Worker fail-closed 返回 `503 auth_oauth_unavailable`，不读取
无界请求体、不转发 legacy，也不代表生产迁移完成。

实现状态（2026-09-01）：Auth/Jobs exact-route staging owner 已启用，manifest
已更新为 staging-owned。`AUTH_EXACT_NATIVE_STAGING_ENABLED` 和
`AUTH_EXACT_OAUTH_STAGING_ENABLED` 负责 Edge → Worker 路由选择；Auth 的
`LEGACY_AUTH_EXACT_STAGING_ENABLED`、Jobs 的
`LEGACY_EXTERNAL_APP_OAUTH_STAGING_ENABLED` 以及所需 secrets 缺失时仍 fail-closed。
因此“代码已可运行”和“旧客户端已完成 production cutover”是两个独立结论。

| 入口                           | 当前 owner | 不能单独切换的 state/side effect                                                       |
| ------------------------------ | ---------- | -------------------------------------------------------------------------------------- |
| `GET /v1/auth/authorize`       | Auth staging | Redis 五分钟 auth session、native/loopback redirect、PKCE 和 provider redirect         |
| `GET /v1/auth/callback/google` | Auth staging | 消费 auth session、Google code exchange、一次性 auth code 和 callback HTML             |
| `POST /v1/auth/callback/apple` | Auth staging | form-post、Apple 首次 `user` name、一次性 auth code 和 callback HTML                   |
| `POST /v1/auth/token`          | Auth staging | single-use code、redirect/PKCE 校验、Firebase provider credential/custom token         |
| `GET /v1/oauth/authorize`      | Jobs staging | D1 app catalog、consent HTML、CSRF cookie；页面必须与 token transaction 配对           |
| `POST /v1/oauth/token`         | Jobs staging | Firebase ID-token verify、private/paid/tester/setup admission、enable/install mutation |

证据来源：`backend/routers/auth.py`、`backend/routers/oauth.py`，以及桌面端
`desktop/windows/src/main/auth/omiAuth.ts`、macOS `AuthService.swift` 和移动端
`app/lib/services/auth_service.dart`。旧客户端明确要求
`custom_token`，之后调用 Firebase `signInWithCustomToken`；把 Better Auth
cookie 或 JWT 放进该字段不能算 wire compatibility。

## Required D1 authority design

下面是实现与生产切换前必须评审的 schema 形状。迁移
`auth/0007_legacy_compatibility_authority.sql` 与
`auth/0009_legacy_auth_transaction_metadata.sql` 声明这些表；namespaced 与
exact staging adapter 已读取 auth D1 transaction authority 并承载 staging
owner。只有下面的 adapter/fixture 和真实 provider replay 全部通过后才可启用生产切换。

### 1. Firebase identity continuity

保留现有 Better Auth `user.id` = Firebase `localId` 的导入策略，并在 auth D1
增加明确的 migration projection（名称可调整，但不能只依赖 email）：

```sql
cf_firebase_identity_projection(
  firebaseUid TEXT PRIMARY KEY,
  betterAuthUserId TEXT NOT NULL UNIQUE REFERENCES user(id),
  providersJson TEXT NOT NULL, -- JSON array; provider account ids remain imported data
  sourceImportId TEXT NOT NULL REFERENCES auth_identity_imports(id),
  status TEXT NOT NULL CHECK (status IN ('imported', 'revoked', 'conflict')),
  sourceUpdatedAt INTEGER NOT NULL,
  updatedAt INTEGER NOT NULL,
  sourceRecordSha256 TEXT -- nullable only for rows created before migration 0010
)
```

`scripts/import-firebase-identities.mjs` 和 `auth_identity_imports` 已提供受控
导入/校验基础；现在 `validate → apply → verify` 还会为每个导入用户写入并
校验该 projection，以及 generation=1、status=`clear` 的
`cf_auth_deletion_fences`。只有 `status=completed`、source/config/canonical
checksum 一致、所有支持的 provider 均配置且没有 conflict/revoked principal，
才允许该 projection 作为 legacy auth 的身份准入。未出现在 projection 的旧 UID
必须返回不可用，而不能创建一个新的 Better Auth 用户或按 email 猜测合并。

Migration `0010_firebase_identity_provenance.sql` additionally records a
deterministic SHA-256 digest for each imported user plus its Better Auth account
rows. The digest contains no Firebase export data and is checked during
`apply`/`verify`; pre-0010 rows remain nullable only so the same-source import
can backfill them after the full user/account checksum has passed. A conflicting
non-null digest is never overwritten. The custom-token bridge requires a valid
per-row digest, so an unverified projection fails closed until it is reconciled.

### Firebase custom-token bridge（staging-only）

`workers/auth/firebase-custom-token-bridge.ts` 已实现一个不改变 legacy owner 的
namespaced bridge。它从 service-account JSON 通过 Workers Web Crypto 的 RSA-SHA256
签名生成短时（默认 300 秒）Firebase custom token；可选的
`FIREBASE_API_KEY` 只用于固定的 Identity Toolkit REST
`accounts:signInWithCustomToken` exchange。token 明文不写入 D1，只保存发行 id 和
SHA-256 hash。发行前必须同时满足 completed import、imported projection 和明确
的 clear deletion fence，且 token claims 绑定当前 account generation。

Auth Worker 的 `POST /internal/firebase/custom-token` 仅在
`FIREBASE_CUSTOM_TOKEN_BRIDGE_STAGING_ENABLED=true` 时出现，并且只接受带
`audience=auth`、`authority=internal` 的 signed context；uid 由 context 绑定，不能
由请求体跨账号指定。缺少 service-account/API key、projection、generation 或
deletion fence 时统一 fail-closed。该 endpoint 仍是内部验证 seam，不是
`/v1/auth/token` owner，也没有宣称旧 Redis callback/PKCE/provider response parity。
配置项为 `FIREBASE_SERVICE_ACCOUNT_JSON`、可选 `FIREBASE_PROJECT_ID`、可选
`FIREBASE_API_KEY` 和 `FIREBASE_CUSTOM_TOKEN_TTL_SECONDS`；默认 wrangler 配置不
打开 staging gate。

### 2. Native auth compatibility transaction

将 Redis `auth_session:*` 和 `auth_code:*` 迁移成 auth D1 的短 TTL、hash-only
transaction；credential payload 必须使用 Auth Worker 的加密 envelope，不能把
Google/Apple access token 明文写入 D1 日志或普通列：

```sql
cf_legacy_auth_transactions(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('session', 'code')),
  provider TEXT NOT NULL CHECK (provider IN ('google', 'apple')),
  lookupHash TEXT NOT NULL UNIQUE, -- hash(state) for session; hash(code) for code
  stateHash TEXT NOT NULL,         -- binds a code transaction to its auth session
  redirectUri TEXT NOT NULL,
  codeChallenge TEXT NOT NULL,
  codeChallengeMethod TEXT NOT NULL CHECK (codeChallengeMethod = 'S256'),
  encryptedPayload TEXT,
  metadataEnvelopeEnc TEXT, -- v1.* AES-GCM caller redirect/state for sessions
  status TEXT NOT NULL CHECK (status IN ('pending', 'consumed', 'failed')),
  expiresAt INTEGER NOT NULL,
  createdAt INTEGER NOT NULL,
  consumedAt INTEGER
)
```

`lookupHash`/`stateHash` 必须以 provider、redirect URI 和 transaction id 绑定；
raw state、raw auth code、raw CSRF 和 provider token 都不能写入 D1；
消费使用 `DELETE ... RETURNING` 或等价的原子状态转移。回调 HTML、Apple
form-post、redirect scheme allowlist 和 PKCE verifier 都必须保持旧客户端可观察
的行为。这个表本身不能解决 Firebase custom-token 签发，它只解决 Redis
transaction authority。

#### Namespaced native-auth seam（staging-only）

上述 transaction authority 现在有一个不改变 owner 的最小可执行 seam：
`workers/auth/native-auth-compatibility.ts` 注册
`/v2/cf/auth/authorize`、`/v2/cf/auth/callback/google`、
`/v2/cf/auth/callback/apple` 和 `/v2/cf/auth/token`。它只在
`LEGACY_AUTH_COMPAT_STAGING_ENABLED=true` 且 provider、HTTPS public base URL
及 `LEGACY_AUTH_TRANSACTION_ENCRYPTION_SECRET` 均已配置时启用；没有这些配置时
统一 fail-closed，不调用 provider。

authorize 创建五分钟 D1 session transaction，session 的 caller redirect/state
使用 `auth/0009_legacy_auth_transaction_metadata.sql` 新增的
`metadataEnvelopeEnc` AES-GCM envelope 保存；state/code secret 仅保存 hash，
而 provider credential 只进入加密 envelope（经过校验的 redirect URI 和 PKCE
challenge 仍是 transaction protocol metadata）。
Google GET 和 Apple form-post callback 先原子消费 session，再通过可注入的
provider fetch exchange code，并把 provider credential 放进 code envelope。
token 以 redirect + S256 verifier 原子消费 code 后返回 provider credential-only
响应。provider 授权 URL 保持 legacy wire，不发送无法在后续 exchange 提供的
客户端 PKCE challenge；PKCE 仍绑定 Omi 自己的 auth code。

由于 D1 没有 Redis 式 TTL，native-auth seam 在 authorize、provider callback 和
token admission 时执行最多 100 行的有界过期清理：过期的 pending session/code
以及已过期的 consumed/failed transaction 都会被删除，按
`(expiresAt, id)` 确定性排序，不触碰仍在有效期内的 transaction。清理失败不会
放宽认证校验或阻止当前请求；它只会留下下一次 admission 可回收的 maintenance
残留。provider credential 仍只存在于 AES-GCM envelope，清理不会把其明文读出。

Auth Worker 现在注册并承载 exact `/v1/auth/authorize`、Google/Apple callback 和
`/v1/auth/token` handler；它们与 namespaced seam 共用同一套 D1 transaction
authority，并由 `LEGACY_AUTH_EXACT_STAGING_ENABLED` 独立 gate。Google 使用
`GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET`；Apple 使用静态
`APPLE_CLIENT_SECRET`，或由 `APPLE_TEAM_ID`、`APPLE_KEY_ID`、
`APPLE_PRIVATE_KEY` 在 Workers Web Crypto 中生成短时 ES256 client-secret JWT。
Edge 在 `AUTH_EXACT_NATIVE_STAGING_ENABLED=true` 时把 exact 请求转给 Auth
Worker；该 wiring 已覆盖 route-level 回归测试，但没有宣称真实 provider
production parity。

`use_custom_token=true` 现在仅在配置 Firebase bridge 且 identity projection
通过时尝试正向闭合：Auth Worker 通过 Identity Toolkit provider exchange，使用
`FIREBASE_SERVICE_ACCOUNT_JSON` 签发短时 custom token，并以
`FIREBASE_API_KEY`/`FIREBASE_PROJECT_ID` 及 `cf_firebase_identity_projection`
校验 Firebase localId、Better Auth uid、generation 和删除 fence；缺少任一
authority 或 secret 都明确返回 `503`/`409`，不会猜测或伪造 uid。Auth 的
`/internal/verify` 与 `/internal/verify-firebase` 也只接受 API-key 验证过的
Firebase ID token，并要求 imported projection。该 seam 不是 `/v1/auth/*` owner、
不是 Edge manifest route，也没有声明真实 Firebase sign-in 或生产 identity
continuity；focused coverage 位于
`tests/native-auth-compatibility.test.ts`（正向 mock provider、redirect/PKCE、
replay/expiry、provider failure 和 secret gate）。其中 exact surface 也覆盖了
Apple 的 `form_post` 首次 `user` name、callback HTML/redirect、一次性 code 以及
legacy 固定 `expires_in=3600` 的响应约束，并验证 Apple 动态 client-secret JWT
header/claims；这些是 mock-provider response conformance，不等于真实 provider
replay 或生产 identity continuity 已闭合。

### 3. External app OAuth transaction

`/v1/oauth/*` 需要 app D1 与 auth D1 之间明确的用户身份边界；不能复用
Better Auth MCP 的 `oauthClient`/consent 表，因为后者是“外部 MCP client 调用
Omi server”，而这里是“用户授权 Omi app 并触发 app install”：

```sql
cf_external_oauth_transactions(
  id TEXT PRIMARY KEY,
  appId TEXT NOT NULL, -- app catalog lives in App D1; cross-DB revision is required
  uid TEXT NOT NULL,
  stateHash TEXT NOT NULL UNIQUE,
  csrfHash TEXT NOT NULL UNIQUE,
  redirectUrl TEXT NOT NULL,
  appCatalogRevision INTEGER NOT NULL,
  appPolicyJson TEXT NOT NULL,
  setupTargetHash TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending', 'consumed', 'failed')),
  expiresAt INTEGER NOT NULL,
  createdAt INTEGER NOT NULL,
  consumedAt INTEGER
)
```

所有用户相关的 dormant transaction 都必须先经过同一个删除 fence。Auth D1
中的 `cf_auth_deletion_fences(uid, generation, status, startedAt, completedAt)`
只有 `status='clear'` 才允许创建或消费；缺行视为 authority unknown，
`deleting/deleted` 一律拒绝。Auth Worker 的内部 identity deletion 现在会在
删除 Better Auth/projection rows 前激活该 fence，并在 identity residual 清零后
标记 `deleted`；Jobs 仍负责跨 App-D1/R2 的 quiescence、清理和最终 tombstone，
所以这不等于整个账号删除闭环已经完成。

`cf_app_catalog.data_json` 还必须经过 schema-versioned projection，覆盖
`external_integration.app_home_url`、`private`、`is_paid`、setup callback policy
和 capability/action scopes；`cf_user_enabled_apps`、subscription entitlement
和 install counter 必须由同一 D1 mutation 以 uid-scoped CAS 更新。setup URL
只能经过现有 SSRF-safe public HTTPS policy，并且 provider timeout/failure 不能
留下已启用 app 或递增 installs 的半成品。

### Namespaced external-app consent seam (staging-only)

`workers/jobs/external-app-oauth-staging.ts` now provides the independently gated
`GET /v2/cf/oauth/authorize` and `POST /v2/cf/oauth/token` seam. The Edge route
requires a Better Auth bearer context and forwards it to Jobs; the Jobs route is
served only when `EXTERNAL_APP_OAUTH_STAGING_ENABLED=true`. The same handler has an
exact `/v1/oauth/authorize` and `/v1/oauth/token` surface behind
`LEGACY_EXTERNAL_APP_OAUTH_STAGING_ENABLED`; Edge enables that proxy only with
`AUTH_EXACT_OAUTH_STAGING_ENABLED=true`. The exact page loads Firebase Web Auth and
posts a bounded multipart `firebase_id_token`; Jobs verifies it through Auth's
projection-backed Identity Toolkit boundary before mutating app install state. This
is owner-ready staging wiring, not a manifest owner change.

The authorize response creates a ten-minute D1 transaction in
`cf_external_app_oauth_transactions`. Only SHA-256 hashes of the random state and
CSRF values are persisted. The browser receives the same CSRF value in an
HttpOnly, Secure, SameSite=Strict cookie and in a form field; token exchange
requires an exact constant-time match, the authenticated uid, the app id, an
unexpired pending transaction, and a current app-catalog revision. Duplicate form
fields, oversized/non-form bodies, unsafe app/setup HTTPS targets, and account
deletion intents/tombstones fail closed. Multipart Firebase forms are bounded
from the raw request stream before Worker form parsing, including requests that
omit `Content-Length`; a declared or observed body above the 16 KB limit is
rejected without invoking the Auth identity verifier.

Token exchange performs setup callback and paid-entitlement checks before the
uid-scoped enabled-app insert. The install and public install-counter update are
revision-checked; a stale catalog or deletion fence cannot be used to authorize a
new install. Both surfaces return the legacy-compatible `{uid, redirect_url, state}`
shape; the exact surface keeps the legacy cookie name, Firebase form flow and
FastAPI-style `{detail: ...}` errors. Focused coverage is in
`tests/external-app-oauth-staging.test.ts`, including an ID-token-sized credential
and single-use install transaction. A disposable Firebase account, real setup
callback, subscription fixture, catalog backfill, and concurrent install probe are
still required before any exact-route owner/manifest change.

## Required fixture matrix

`workers/auth/legacy-compatibility.ts` 仍提供 dormant transaction/admission seam；
`firebase-custom-token-bridge.ts` 另外覆盖 service-account signing、D1 issuance
hash、generation/deletion-fence gate 和可选 Firebase REST exchange，但不执行
legacy callback 或 app enable/install。对应 fixture 通过这些生产 adapter seam；
在任何 owner 变更前，仍必须补齐 Auth Worker/Edge 的完整 provider handler replay，
不能只检查源代码字符串：

1. Identity import：完整 imported UID 正向；missing UID；重复 email；重复
   provider identity；provider account relink on replay；disabled user；
   phone/custom-claims user；checksum 或 provider configuration mismatch；
   revoked/conflict principal。
2. Native authorize/callback：Google 和 Apple 正向；unsupported provider；
   redirect scheme/host 越界；missing/malformed PKCE；provider denial/error；
   expired/unknown/replayed state；Google GET callback；Apple POST callback，
   包含首次 name 与无 name 两种情况。
3. Native token：正确 redirect + S256 verifier；redirect mismatch；wrong or
   missing verifier；consumed/expired/malformed code；provider credential
   failure；Firebase bridge unavailable；service-account malformed/rotation；
   account-generation mismatch；deletion fence race；Firebase REST 4xx/5xx 和
   malformed response；`use_custom_token=true/false` 的 exact response fixture；
   旧 UID 未导入时必须 fail-closed。
4. App consent：unknown/disabled/private/paid app；tester vs non-tester；setup
   callback success/timeout/non-JSON/unsafe redirect；missing/mismatched CSRF；
   cross-user transaction；replay；concurrent enable/install counter；account
   deletion fence。测试必须证明 app catalog、subscription 和 enabled-app mutation
   是同一个 uid authority。
5. Boundary regression：六个 exact routes 在 staging fail-closed 下均为 503，
   request body 不被读取，`globalThis.fetch` 不触发 legacy backend；关闭开关的
   非 staging fallback 另有 regression，避免把 staging guard 误部署为永久
   protocol replacement。

## Staging admission order

1. 在隔离 Auth D1 上运行 Firebase export `validate → apply → verify`，保存
   ledger/checksum 和 row-count 证据；不使用生产 token 或用户数据做本地 fixture。
2. 配置仅 staging 的 Google/Apple/Firebase bridge secrets，并用 disposable
   account 做完整 provider callback → namespaced custom-token bridge → Firebase
   sign-in 试验；必须证明 UID 与 D1 projection/generation 一致，且旧客户端能
   解析所有字段。当前 service-account/API key 未配置时只允许验证 fail-closed。
3. 先在 staging 让 namespaced 与 exact compatibility endpoint 命中 Auth Worker，
   验证 callback HTML、single-use/PKCE、provider failure 和 deletion fence；期间
   不要把 Better Auth session alias 到 Firebase custom token。
4. 对 app OAuth 先回放一个非付费、非私有 disposable app，再覆盖 paid/private/
   setup callback 失败和 install CAS；验证旧 backend 未被调用、D1 residual 为零。
5. 只有上述正向与失败探针均通过，才可从 staging owner 进入 production rollout，
   并保留 `AUTH_OAUTH_STAGING_FAIL_CLOSED` 作为回滚闸门。
