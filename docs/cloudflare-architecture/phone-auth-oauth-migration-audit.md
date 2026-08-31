# Phone / Twilio 与 Auth / OAuth 迁移审计

审计日期：2026-08-31

## 结论

当前没有一组可以安全直接切换 owner 的剩余 Phone/Twilio 或 legacy Auth/OAuth 路由。

Phone 中最接近闭环的是 `POST /v1/phone/token`：Workers 可以用 Web Crypto 生成 Twilio Voice Access Token，且不需要调用 Twilio API。但它仍必须先读取“该 uid 是否有 verified caller ID”的权威数据；现在这份数据在 Firestore，Cloudflare 没有 phone schema、历史回填或删号残留 fence。因此只迁移 token 签发会把没有历史号码的用户误判为未配置，或者为了返回成功而绕过 caller-ID 检查，二者都不是 parity。

Auth/OAuth 不能用 Better Auth session 直接替换。Better Auth 已在 D1 上承载新会话、Google/Apple social sign-in 和 MCP OAuth，但 desktop/CLI 的 legacy `/v1/auth/token` 合约返回 Firebase custom token，并由客户端继续向 Firebase mint ID/refresh token；legacy app OAuth 还依赖 Firestore app 文档、启用/付费状态和外部 setup callback。两个身份系统目前没有可验证的 Firebase UID ↔ Better Auth user link/import authority。

因此本轮只记录可执行的迁移边界，不减少 `legacy-owned` 计数，也不把现有 staging fail-closed 保护称为迁移完成。

## Phone / Twilio 路由证据

当前清单中的六条 Phone 路由为：

| 路由 | legacy authority | 不能直接切换的原因 |
| --- | --- | --- |
| `GET /v1/phone/numbers` | `users/{uid}/phone_numbers` Firestore 子集合，读取时还受 data-protection 解密影响 | 没有 `cf_phone_numbers` D1 projection；直接返回空列表会丢失历史 verified numbers |
| `DELETE /v1/phone/numbers/{phone_number_id}` | Firestore 文档 + Twilio `outgoing_caller_ids(sid).delete()` | 需要 uid-scoped D1 CAS、Twilio 删除重试/`already_deleted` 终态和 account-deletion residual fence |
| `POST /v1/phone/numbers/verify` | Redis rate limit/quota、Firestore duplicate check/pending state、Twilio validation request | provider 调用成功后 Firestore 写入可能失败；需要幂等 verification job、状态 TTL 和失败补偿，不能由请求重试重复拨号 |
| `POST /v1/phone/numbers/verify/check` | Firestore pending UID + Twilio caller-ID lookup + Firestore verified record | 必须阻止跨 uid claim，并在并发 poll 下原子创建唯一号码/primary；当前没有 D1 uniqueness 或 verification state |
| `POST /v1/phone/token` | Firestore primary number + Twilio Voice JWT（API key SID/secret、account SID、TwiML app SID） | JWT 算法可在 Workers 实现，但 caller-ID authority 和 account/deletion fence 缺失；不能无条件签发 token |
| `POST /v1/phone/twiml` | Twilio `X-Twilio-Signature`、Firestore primary number、Redis monthly quota、Twilio lookup、原子 quota reservation | 需要按 Twilio 规范校验原始 URL/form、D1/DO 原子配额、caller-ID freshness 和 bounded XML response；不能把签名验证或计费降级成普通 proxy |

对应实现证据：

- `backend/routers/phone_calls.py:109-242` 展示号码验证、跨用户 pending 检查、primary 号码检查和 token admission；`backend/routers/phone_calls.py:250-363` 展示 TwiML 的签名、caller-ID、套餐 allowlist 和 quota reservation 顺序。
- `backend/database/phone_calls.py:45-192` 说明 Firestore 子集合、enhanced protection 加密/hash、pending verification TTL 和 primary fallback。
- `backend/utils/twilio_service.py:53-87,90-148,151-179,246-273` 说明 Twilio JWT、REST caller-ID lookup/delete 与签名验证所需的 secrets 和 provider 语义。
- `backend/utils/phone_calls.py:100-218` 说明套餐、月度计数、目的地 allowlist 以及免费用户原子 reserve；它不是可在 Workers 端直接复用的 D1 authority。

当前 `deploy/cloudflare/migrations/app/` 没有 phone number、pending verification 或 phone usage 表；`deploy/cloudflare/workers/jobs/env.ts` 也没有 Twilio account/API key/TwiML app secret binding。现有 `PHONE_TWILIO_STAGING_FAIL_CLOSED` 只阻止 isolated staging 将凭据、号码状态和电话指令送入 legacy，并不提供业务处理。

### 可验证的最小迁移顺序

1. 新增 `cf_phone_numbers`、`cf_phone_verifications`、`cf_phone_call_usage` 和 phone control/deletion-fence 表。号码明文应使用 uid-bound envelope 加密，查询使用 hash；`twilio_sid`、verification status、`is_primary`、generation 和删除状态必须同一 authority 管理。
2. 先做受控 Firestore backfill 和 residual audit，再实现 `GET`/`DELETE`。backfill 不完整时，route 必须对未投影用户返回明确 unavailable，而不能返回空成功。
3. 在 D1 号码投影和 Better Auth uid 认证均具备后，先切 `POST /v1/phone/token`。用 Workers Web Crypto 复现 Twilio Voice JWT 的 header/claims/grant，测试 primary gate、secret 缺失、删除 fence 和 JWT TTL；这条路线不需要先把验证拨号迁过去。
4. 再迁移验证 admission/check：Twilio REST 应由 bounded fetch 调用，D1 写入与 Queue retry/idempotency 分离，provider 成功但 D1 失败必须进入可重试状态，不能再次无条件发起验证呼叫。
5. 最后迁移 TwiML。保留 Twilio 原始 form 字段和 canonical URL 的签名验证，使用 D1/DO 原子 reservation；要覆盖 malformed signature、重复 `CallId`、quota race、unknown destination、caller-ID 已被 Twilio 删除和删号中的请求。

## Auth / OAuth 路由证据

相关 legacy 入口包含 `/v1/auth/authorize`、`/v1/auth/callback/google`、`/v1/auth/callback/apple`、`/v1/auth/token`、`/v1/oauth/authorize`、`/v1/oauth/token`；`/v1/apps/mcp` 与 `/v1/apps/mcp/callback` 还依赖 Persona/apps 的外部 MCP app authority。

- `/v1/auth/authorize` 以 Redis `set_auth_session` 保存 5 分钟 flow，校验 native/loopback redirect 和 PKCE S256，然后跳转 Google/Apple。callback 再调用 provider token endpoint，把 provider credential 放入一次性 Redis auth code。
- `/v1/auth/token` 消费一次性 Redis code，比较绑定的 `redirect_uri` 和 PKCE verifier，返回 provider credential，并可调用 Firebase Admin 生成 `custom_token`。CLI/desktop 随后调用 Firebase `signInWithCustomToken`，所以返回 Better Auth session cookie 并不兼容现有客户端 wire contract。
- `/v1/oauth/authorize` 读取 legacy Firestore app/config 并渲染 consent 页面与 CSRF cookie；`/v1/oauth/token` 还会校验 Firebase ID token、private/paid app、setup-completed URL，并写入 app enable/install 状态。D1 app catalog projection 目前没有证明这些字段和 mutation 已完全等价。
- `/v1/apps/mcp` 会发现远端 MCP metadata/tools，动态注册 OAuth client，把 `client_secret`/`code_verifier` 放入 pending app；callback 交换 token 后再次发现 tools 并更新 app。现有 Better Auth MCP OAuth 的 `oauthClient`/grant 表服务的是外部 MCP client → Omi MCP server，不是这个“把远端 MCP server 安装成 Omi app”的流程。

Cloudflare 现有 `workers/auth/index.ts` 的 Better Auth 已具备 D1 session、social provider、PKCE state 和 MCP OAuth；`workers/jobs/task-integrations.ts`、`google-calendar.ts` 也提供了 D1 state + provider token exchange 的可复用模式。但这些能力不能自动证明 Firebase continuity、legacy custom-token 输出、app consent/install CAS 或 MCP app tool-discovery parity。

### Auth/OAuth 切换前置条件

1. 建立不可歧义的 Firebase UID 与 Better Auth user ID link/import 表，包含 provider、首次/最近登录、冲突处理、撤销和 account deletion fence；未经回填的旧 principal 必须 fail-closed。
2. 选定客户端迁移契约：要么所有 CLI/desktop/mobile 改为 Better Auth token/session，要么在 auth Worker 中实现经过审核的 Firebase custom-token bridge。后者需要 auth worker 的 Firebase service-account/API secrets、JWT signing/rotation、Firebase account lookup 和重复登录/绑定语义，不能只转发 session cookie。
3. 为 app OAuth 建立 D1 app authority（external integration、private/paid/setup 状态、owner CAS、public cache invalidation）及 bounded external callback state；MCP app 还需 R2 logo/provider secret 生命周期、tool manifest 规范和历史 app 回填。
4. 补齐 callback/token 的 redirect/PKCE/state replay、provider error、cross-user app、deletion、provider timeout/retry 和 exact response/html tests；通过 authenticated staging positive flow 后，才可改 Edge 和 route manifest owner。

## 当前边界与验证

Edge 当前在 `PHONE_TWILIO_STAGING_FAIL_CLOSED=true` 时对六条 Phone 入口返回 `503 phone_twilio_unavailable`，在 `AUTH_OAUTH_STAGING_FAIL_CLOSED=true` 时对六个 Auth/OAuth 兼容入口返回 `503 auth_oauth_unavailable`，均不读取 body、不调用 legacy。该边界是凭据隔离和风险控制，不是成功响应替代品。

在具备上述 schema、历史回填、secret binding 和 authenticated fixture 之前，不应把任何 Phone/Auth/OAuth 路由标记为 `staging-owned`。当前最小可执行实现面是：先完成 phone D1 projection/backfill 和 Firebase↔Better Auth link authority，再单独评审 token-only slice；本审计未修改生产 owner 或 route manifest。
