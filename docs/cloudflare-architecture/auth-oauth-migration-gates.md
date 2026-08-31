# Auth / OAuth Cloudflare migration gates

审计日期：2026-08-31

本文只定义下一次正向迁移所需的 authority、schema 和 fixture，不改变当前
legacy owner。Better Auth 官方支持 Cloudflare Workers/Hono 与 D1；仓库中的
Auth Worker 已经使用这条组合承载 `/api/better-auth/*`。这里的阻塞点不是
Workers 不能运行 Better Auth，而是 Better Auth 的 session/OAuth wire 与 Omi
现有 Firebase 客户端协议不同。

## 当前 route decision

以下六条入口继续保持 legacy-owned，并由 Edge 在
`AUTH_OAUTH_STAGING_FAIL_CLOSED=true` 时统一返回 `503 auth_oauth_unavailable`。
该 boundary 不读取请求体、不转发 legacy，也不代表成功迁移。

| 入口 | 当前 owner | 不能单独切换的 state/side effect |
| --- | --- | --- |
| `GET /v1/auth/authorize` | legacy | Redis 五分钟 auth session、native/loopback redirect、PKCE 和 provider redirect |
| `GET /v1/auth/callback/google` | legacy | 消费 auth session、Google code exchange、一次性 auth code 和 callback HTML |
| `POST /v1/auth/callback/apple` | legacy | form-post、Apple 首次 `user` name、一次性 auth code 和 callback HTML |
| `POST /v1/auth/token` | legacy | single-use code、redirect/PKCE 校验、Firebase provider credential/custom token |
| `GET /v1/oauth/authorize` | legacy | Firestore app catalog、consent HTML、CSRF cookie；页面必须与 token transaction 配对 |
| `POST /v1/oauth/token` | legacy | Firebase ID-token verify、private/paid/tester/setup admission、enable/install mutation |

证据来源：`backend/routers/auth.py`、`backend/routers/oauth.py`，以及桌面端
`desktop/windows/src/main/auth/omiAuth.ts`、macOS `AuthService.swift` 和移动端
`app/lib/services/auth_service.dart`。旧客户端明确要求
`custom_token`，之后调用 Firebase `signInWithCustomToken`；把 Better Auth
cookie 或 JWT 放进该字段不能算 wire compatibility。

## Required D1 authority design

下面是实现前必须评审的 schema 形状。迁移 `auth/0007_legacy_compatibility_authority.sql`
现在以 dormant 形式声明这些表，但没有任何 exact legacy route 读取它们，也没有
改变 route owner；只有下面的 adapter/fixture 和真实 provider replay 全部通过后才可启用。

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
  updatedAt INTEGER NOT NULL
)
```

`scripts/import-firebase-identities.mjs` 和 `auth_identity_imports` 已提供受控
导入/校验基础，但只有 `status=completed`、source/config/canonical checksum
一致、所有支持的 provider 均配置且没有 conflict/revoked principal，才允许
该 projection 作为 legacy auth 的身份准入。未出现在 projection 的旧 UID 必须
返回不可用，而不能创建一个新的 Better Auth 用户或按 email 猜测合并。

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
`deleting/deleted` 一律拒绝。这个 fence 目前尚未接入旧账号删除流程，因此它
是准入条件而不是已经完成的删除闭环。

`cf_app_catalog.data_json` 还必须经过 schema-versioned projection，覆盖
`external_integration.app_home_url`、`private`、`is_paid`、setup callback policy
和 capability/action scopes；`cf_user_enabled_apps`、subscription entitlement
和 install counter 必须由同一 D1 mutation 以 uid-scoped CAS 更新。setup URL
只能经过现有 SSRF-safe public HTTPS policy，并且 provider timeout/failure 不能
留下已启用 app 或递增 installs 的半成品。

## Required fixture matrix

`workers/auth/legacy-compatibility.ts` 目前只提供 dormant adapter seam：它执行
hash-only transaction、S256/redirect 校验、uid/CSRF 原子消费、Firebase projection
admission、public-HTTPS setup target 和 deletion-fence gate，但不调用 provider，
不签发 Firebase custom token，也不执行 app enable/install。对应 fixture 通过
该生产 adapter seam；在任何 owner 变更前，仍必须补齐 Auth Worker/Edge 的完整
provider handler replay，不能只检查源代码字符串：

1. Identity import：完整 imported UID 正向；missing UID；重复 email；重复
   provider identity；disabled user；phone/custom-claims user；checksum 或
   provider configuration mismatch；revoked/conflict principal。
2. Native authorize/callback：Google 和 Apple 正向；unsupported provider；
   redirect scheme/host 越界；missing/malformed PKCE；provider denial/error；
   expired/unknown/replayed state；Google GET callback；Apple POST callback，
   包含首次 name 与无 name 两种情况。
3. Native token：正确 redirect + S256 verifier；redirect mismatch；wrong or
   missing verifier；consumed/expired/malformed code；provider credential
   failure；Firebase bridge unavailable；`use_custom_token=true/false` 的 exact
   response fixture；旧 UID 未导入时必须 fail-closed。
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
   account 做完整 provider callback → token → Firebase sign-in 试验；必须证明
   UID 与 D1 projection 一致，且旧客户端能解析所有字段。
3. 先让 namespaced compatibility endpoint 命中 Auth Worker，验证 callback HTML、
   single-use/PKCE、provider failure 和 deletion fence，再讨论 exact `/v1/auth/*`
   owner；期间不要把 Better Auth session alias 到 Firebase custom token。
4. 对 app OAuth 先回放一个非付费、非私有 disposable app，再覆盖 paid/private/
   setup callback 失败和 install CAS；验证旧 backend 未被调用、D1 residual 为零。
5. 只有上述正向与失败探针均通过，才可在一次变更中同步更新 Edge route、
   `backend-routes.json` 和 `routes.yaml`；否则继续使用
   `AUTH_OAUTH_STAGING_FAIL_CLOSED`。
