# 08 · M2 落地时发现、未在同一 PR 处理的 Cloudflare 缺口

> 背景：GitHub Issues 在这个 fork 上被禁用（`gh issue create` 返回 "the ... repository has disabled issues"），追踪只能走仓库内文档，与 `upstream-prs.md` 是同一惯例。以下每条都是 M2 落地（`03-deploy-targets.md` §3 契约对齐）执行时实测发现、但判断为"新写安全敏感代码"或"依赖 B0 尚未落地"而特意不塞进同一个 PR 的项。每条给出足够的定位信息（文件:行号），接手时不必重新调研。

## 1. Edge 对 bearer JWT 不做本地 JWKS 校验（D2 后半）

`workers/edge/auth.ts:9-33` 的 `verifyBearer` 是 Edge 唯一的校验路径，bearer token 和 cookie 一视同仁，每次都经 service binding 往返 Auth 的 `POST /internal/verify`（`workers/auth/index.ts:857-945`）。

D2 要的是 Edge 对 bearer JWT 本地 JWKS 校验，`/internal/verify` 只留给 cookie 会话。脚手架已经在但没人用：`workers/edge/env.ts:14-16` 声明了 `BETTER_AUTH_JWKS_URL`/`BETTER_AUTH_ISSUER`/`BETTER_AUTH_AUDIENCE`，全仓零读取点，`wrangler.jsonc`、`.dev.vars.example` 都没声明；`package.json` 没有 `jose` 之类的 JWT 校验依赖；`workers/edge/*.ts` 里搜 `jwks|jose|createRemoteJWKSet|jwtVerify` 零命中。

顺带：Auth Worker 的 JWT 插件（`workers/auth/index.ts:215-224`）硬编码 `expirationTime: "24h"` 和两个 rotation 常量（30 天/2 天），没有显式 `iss`/`aud` 声明（吃库默认值）。D2 要 TTL 可配、默认 3600s。

**为什么没做**：这是要新写的、直接碰凭证校验边界的代码（拉取并缓存 JWKS、验 ES256 签名、处理 key 轮换），不是改名或接默认值。仓库自己的 failure-class 清单（`FC-client-rederives-authority-verdict`、`FC-identity-fallback-used-as-authorization-gate`）就是为了防止这类改动被赶工。今天的机制（service binding 往返）仍然正确、安全——Workers 的 service binding 是同 isolate 调用，不是真实网络往返——只是没照 D2 的字面偏好实现。

## 2. web-ticket / bootstrap header 没有撤回，`/v4/web/listen` 没有回到上游单跳协议（D2）

CF 给 `/v4/web/listen` 发明了一条三跳链路，取代上游单跳的 `{"type":"auth","token":"<真实 JWT>"}`：

- **上游真实协议**：`backend/routers/transcribe.py:221-259` 的 `web_listen_handler` 接受连接后读第一帧，经 `backend/utils/other/endpoints.py:415-436` 的 `_verify_user_uid_from_ws_message` 校验，要求 `{"type":"auth","token":"<真实 bearer/ID JWT>"}`，一步验证，和别处用的凭证是同一种。
- **CF 现状**：`workers/edge/index.ts:1118-1136` 的 `/v4/web/listen` 处理器**直接丢弃**客户端的 `Authorization` 头（第 1129 行 `headers.delete("authorization")`），改发 `x-omi-realtime-bootstrap`/`x-omi-realtime-bootstrap-signature`（定义于 `workers/shared/realtime-bootstrap.ts:3-5`，30 秒 HMAC 签名）给 Realtime Worker（校验于 `workers/realtime/index.ts:113-134`）；DO 那端的首帧校验（`workers/realtime/session.ts:35-39,66-93,403-421,476-486`）验的是一次性 `ticket`（`workers/shared/realtime-ticket.ts` 定义，30 秒 HMAC），**不是** JWT。`ticket` 由两个额外 REST 端点铸造：`POST /v1/realtime/web-ticket`（`workers/edge/index.ts:1138-1156`）和 `POST /v2/realtime/session`（`workers/edge/index.ts:1937-1998`）。`manifests/backend-routes.json` 里确认没有这两个端点——不是上游路由。

**为什么没做**：这条改动横跨 3 个 Worker（Edge、Realtime 路由、Realtime DO 会话）+ 至少 30 个测试文件里对 ticket 流程的引用（`edge.test.ts:3579` 等），是把一整条鉴权链路换掉，不是配置项。同样落在 identity-chain 类失败模式的高风险区，需要独立 PR、独立复审深度。

## 3. Firebase scrypt 迁移逻辑没有抽到 `auth/shared/`（S6）

现状：只有一处，`workers/auth/firebase-migration-password.ts`（386 行）。约 76%（1-293 行：`parseFirebaseScryptConfig`、`encodeFirebasePasswordHash`、`hashFirebasePassword`、`verifyMigratedFirebasePassword` 等）是纯逻辑，不碰 D1/Hono；只有两个函数（`findOnlyCredentialPassword`、`upgradeMigratedFirebasePassword`，315-385 行）耦合 D1 原始 SQL。Hono/HTTP 接线在 `workers/auth/index.ts:142-176`。

`03-deploy-targets.md` §5 要的 `auth/shared/`（TS 包，JWT 插件参数 + claims 形状 + Firebase scrypt 校验/首登重哈希，`auth-server/` 和 `workers/auth` 各写一个 adapter）全仓不存在——只有 `workers/shared/`（Worker 运行时专用，不是这个）。

**为什么没做**：这是 S6 自己的 PR（`07-pr-plan.md` 里 M2 的依赖列了 S2–S6，但和 M1 一样，M2 没有等全部依赖到位才落地）。现在抽没有第二个消费者验证接口设计对不对——`auth-server/`（自托管的 Express+PG 实现）还没有改成用它。抽取本身工作量不大，风险在"接口形状是否两边都好用"，最好等 S6 单独验证。

## 4. Cloudflare 资源命名没有品牌化，`apply.py` 尚不存在（B-series 缺口）

`04-brand-layer.md` 描述的 `scripts/brand/apply.py`（`--only flutter|desktop|backend|firmware|web|docs|ci`）全仓不存在——没有 `brand.py`、没有 `brand/` 目录、没有 `wrangler.<stage>.jsonc` 命名模式，且它的 `--only` 枚举里**没有 `cloudflare`**，B0 落地后仍需先扩展这个枚举才谈得上覆盖这里。

需要品牌化的具体面：
- **`omi-cf-*` 硬编码资源名**：D1 数据库、R2 桶、Vectorize 索引、Queue、Worker service 名——分布在 7 个 `wrangler.jsonc` + `manifests/resources.yaml` + `manifests/r2-namespaces.yaml` + `manifests/vector-namespaces.yaml` + `scripts/deploy.mjs:223-275`。校验侧也硬编码了前缀：`scripts/validate-manifests.mjs:282,730` 直接 `if (!name.startsWith("omi-cf-")) throw`，改前缀要连校验一起改，不是纯 find/replace。
- **`workers.dev` 子域名**：staging 侧完全硬编码（`summersmile1984` 字面量出现在 `workers/edge/wrangler.jsonc:37-39`、`workers/auth/wrangler.jsonc:12-13`、`workers/jobs/wrangler.jsonc:15`、`python/api-core/wrangler.jsonc:73`，是功能性的值——CORS allowlist、MCP URL——不是装饰性的）。production 侧已经是 M2 本身在这个 PR 系列里修过的部分（见"stop defaulting the production workers.dev subdomain to summersmile1984"这次提交）；staging 侧的硬编码没有对应的运行时安全阀，因为 staging 从不走 `production-environment.mjs` 那条路径，直接读 `wrangler.jsonc` 字面量。
- **`x-omi-*` 内部头名**：调研过（见下），大部分是 Worker 间内部断言头（不出边界），不需要品牌化；只有跨到客户端/第三方 webhook 的那几个（`X-Omi-Rate-Limit-Reason`、`x-omi-chat-*`、`x-omi-sync-capture-manifest` 等）理论上需要，但 `04-brand-layer.md:134` 已经把 `x-omi-sync-capture-manifest` 列为两端契约里明确要保留的那个，说明"是否品牌化头名"本身还没定案，不该在 B0 之前动。

**为什么没做**：B0（品牌 manifest + `apply.py` 骨架）本身还没有一行代码，M2 不该抢跑发明一套只服务 CF 目标、将来要被 B0 推翻重做的品牌注入机制。B-series 表（`07-pr-plan.md` §2）目前没有覆盖 Cloudflare 资源命名的行——建议 B0 落地后新增一条 `B9 | cloudflare: apply.py 扩展 --only cloudflare + omi-cf-* 前缀模板化 + workers.dev 子域名模板化 | B0, M2`。

## 5. `deploy/cloudflare/ci/contract.sh` 不存在，三个已能跑的 npm 脚本没有接进 CI（C4 自己的范围，未提前做）

`deploy/cloudflare/ci/` 目录不存在，仓库里没有任何 `contract.sh`；也没有任何地方真正调用 `wrangler dev`/Miniflare（`vitest.config.ts:12-15` 把 `cloudflare:workers` 别名到一个 9 行的手写 stub，测试是纯 Node 环境下的 hermetic 单测，不是接真实 Workers 运行时/D1 迁移的集成测试）。

`validate:manifest`、`verify:migrations`、`validate:backend-routes` 三个 npm 脚本本身**已经能跑**（`package.json` 里就是这次 M2 用来验证改动的那几条命令），只是没有被任何 CI 工作流调用——`.github/checks-manifest.fork.yaml` 现在只有 4 条与 CF 无关的条目，`.github/workflows/fork-checks.yml` 不碰 `deploy/cloudflare`。

**为什么没做**：`07-pr-plan.md` 表里 C4 本来就排在 M2 之后、依赖 M2，这次刻意没有抢跑——把三个已验证脚本接进 `checks-manifest.fork.yaml` 是机械活，但"从零搭一个真正跑 `wrangler dev` + 本地 D1 迁移的契约套件"是新基建，两者最好在 C4 自己的 PR 里一起做、一起给证据（`fork-contract-cloudflare.yml` 绿）。

## 6. 顺带发现：`deploy/cloudflare/` 没有 Node 版本钉死

`wrangler` (`4.127.0`)、`uv==0.12.3`、Python (`>=3.13`) 都已经和设计目标精确一致（不是巧合——这三个值本来就是 CF 分支已经在用的）。**只有 Node 没有任何钉死**：没有 `.nvmrc`、`package.json` 没有 `engines` 字段；唯一线索是 `wrangler` 自己 `package-lock.json` 里继承来的 `"engines": {"node": ">=22.0.0"}`，不是仓库自己声明的。低风险，建议随手在下一个碰 `deploy/cloudflare/` 的 PR 里补一个 `deploy/cloudflare/.tool-versions`（`wrangler 4.127.0` / `python 3.13` / `node 22`）。
