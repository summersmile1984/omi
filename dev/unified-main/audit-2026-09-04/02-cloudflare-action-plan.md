# Cloudflare target：代码审计、部署验收与行动方案

审计日期：2026-09-04（Asia/Shanghai）。本路结论：**Cloudflare 后端已有较完整实现，单元测试、七个 Worker 打包和本地 Auth 真实运行通过；当前统一 `main` 的完整 Cloudflare 发布仍不通过，不能交付为“同一客户端、两个 target、任意白牌”的成品。** 首要阻塞是失效的路由检查和过时的 Web 构建契约，其次是共享身份、实时协议、品牌资源渲染及共用验收尚未闭环。

本文是三路行动方案的 Cloudflare 分册；另见[标准服务器](01-self-host-action-plan.md)、[白牌终端](03-whitelabel-action-plan.md)及[统一主线索引](../README.md)。以同一条 `main`、短期 PR 分支、`deploy/<target>` 与 `brand/<id>` 两个配置维度推进，不恢复部署专用长期分支。

## 1. 审计边界与证据基线

- 被审源码：`origin/main = d238a85af9d999992d9f0352db682cd9f11fc951`。独立 worktree：`/Users/macstudio/Documents/memweft-worktrees/audit-cloudflare-20260904`，分支 `codex/audit-cloudflare-20260904`。
- 上游比较基线由总审计固定为 `upstream/main = c70152426f22eda60f17f52b7d66ede30089a783`（v0.12.284）；本文的运行检查全部针对上面的 `origin/main`，没有偷偷把上游新提交或旧 CF 分支混入运行目录。
- 阅读 `AGENTS.md`、`AGENTS.fork.md`、`web/app/AGENTS.md`、`deploy/cloudflare/README.md`、`dev/unified-main/{02-deployment-profile,03-deploy-targets,08-m2-cloudflare-followups}.md` 后，逐项对照当前源码。旧文档中的数量、已完成状态和 Web 技术栈不直接作为结论。
- 本轮没有改产品源码、已跟踪锁文件、部署配置或远端资源；没有执行远端部署、创建生产测试账户或访问客户数据。远端操作仅迁移状态查询及公开 readiness。依赖安装和本地 D1 数据在独立 worktree/临时目录；本地 Auth 进程已停止。
- 原始日志与一次性审计探针：`/tmp/memweft-audit-20260904/cloudflare/`。这里的脚本是审计证据，尚不是仓库 CI 的新检查。不要将其“本地能运行”表述成“已接入 CI”。

## 2. 已完成的实际表面

| 表面 | 当前事实 | 代码依据/实测边界 |
|---|---|---|
| Worker 组件 | Auth、Rate Limit、Realtime、Jobs、Edge 五个 TS Worker；Core、AI 两个 Python Worker均可打包 | 七个独立 `deploy --dry-run` 全部 exit 0；并非一个品牌整套部署已完成 |
| 业务实现与内存替身测试 | TS 106 文件 / 775 测试；Python Core 395 / AI 118 测试通过 | TS `vitest.config.ts:5-15` 将 `cloudflare:workers` 替换为 `tests/cloudflare-workers-runtime.ts`，该文件仅提供 9 行 DO 基类；此套件不是实际 workerd/DO/D1 集成测试 |
| Auth 路径 | Better Auth 已挂 `/api/auth` | `workers/auth/index.ts:33,140`；旧文档说 `/api/better-auth` 已过时 |
| 回源参数 | 已改为 `ORIGIN_BACKEND_URL`，没有强制默认回 Omi 后端 | `workers/edge/env.ts:13`、`index.ts:522-546,2425-2431`；参数存在不等于混合部署契约通过 |
| 账户迁移围栏 | 代码对新部署默认关闭 | `workers/edge/cutover.ts:20-25`；现有 staging 配置仍显式打开（`edge/wrangler.jsonc:42-46`），不能把现有 staging 当任意新品牌默认值 |
| 本地数据库 | 空白本地 D1 实际应用 Auth 10 个、App 155 个 SQL 迁移成功 | `local-auth-migrations.log`、`local-app-migrations.log`；最高编号 0152，但有同序列附加迁移，所以文件总数为 155 |
| 本地 Auth 运行 | 真正的 Wrangler/workerd + D1：健康、签名密钥就绪、注册、session 换 JWT、两种凭证内部验证通过 | 无效 bearer、缺内部 secret 均 401；仅本地临时测试账户，未测试浏览器 cookie、轮换、撤销、跨 Worker 实时或 Firebase 迁移 |
| 已有远端服务 | 当前公开 production Edge 和 Web readiness 均 HTTP 200；所报六个内部依赖均 200 | 原有线上实例的健康证据，不包含 Git SHA/构建 provenance，不能据此认证本次 main |

## 3. 关键缺口与可复查定位

### CF-F1：发布前置的路由检查已失效，静态 inventory 也已落后

`deploy/cloudflare/package.json:9` 的 `validate:backend-routes` 调用：

```text
backend/scripts/openapi_runner.sh scripts/export_openapi.py \
  --surface cloudflare-route-inventory --check ../deploy/cloudflare/manifests/backend-routes.json
```

实测 exit 2：当前 `backend/scripts/export_openapi.py:1094` 只接受 `public`、`app-client`、`integration-public`。设计要求迁出的 `deploy/cloudflare/scripts/route_inventory.py` 不存在。`deploy:staging` 和 `deploy:production` 都先执行这一检查，因此从标准命令入口已经被阻塞，无须尝试真正发布才能确认。

与此同时，`npm run validate:manifest` exit 0，只证明现有 `routes.yaml` 与 `backend-routes.json` 等静态清单自洽（`scripts/validate-manifests.mjs:164-225`），不验证当前后端所有已注册路由。

本轮使用当前上游导出器的 `generate_public_openapi()` hermetic bootstrap，随后只读枚举真实 `main.app.routes` 的 `APIRoute` 和 `APIWebSocketRoute`，得到 **612 条实际 HTTP/WS 注册项，对比 manifest 577 条，缺 36 条、陈旧 1 条**。此数字是 inventory 差异，不能推断 36 条的运行实现必然都缺失；部分可能落到现有通配处理器，必须重新判定 owner/语义。

缺项包括 CSAT、frame requests、screen frame egress、JIT、memory ledger/revert、referral、calendar capture gaps、截图/共享等；陈旧项是 `GET /v1/crisp/unread`。完整清单及一次性探针在 `route-compare.log`、`route-compare.py`。新增归属须以当前上游为源，禁止给失效的 `--surface` 在上游脚本中再补 fork 专属分支。

### CF-F2：Web 发布器仍按 Next/vinext，真实上游已变成 Moonshine/Bun

- `web/app/package.json:6-15`：`moonshine build`，`bun .moonshine/server.ts`，测试用 Bun + Vitest；无 `build:vinext:staging` / `build:vinext:production`。`scripts/copy-moonshine-assets.ts:63,285-292` 生成 Bun 服务器。
- 本轮还直接读取 `git show upstream/main:web/app/package.json`，上游同样已采用 Moonshine/Bun；这不是 fork 应该删除的单方偏离。
- `scripts/deploy.mjs:88-111` 仍跑 `next typegen`、vinext 构建并寻找 `dist/server/wrangler.json`。实跑 `cd web/app && npm run build:vinext:staging` exit 1：Missing script。
- 生产入口亦有 `next typegen`（`deploy-production.mjs:93-97`）和 `build:vinext:production`（`:515-535`）。更需修正的是发布顺序：`:627-642` 在生产资源/迁移/后端部署之后才到 `deployWeb()` 的 Web 构建。完整资格检查必须在外部写入之前完成。

因此 `03-deploy-targets.md` §6 的 D3“保留上游 Next，不引入 Bun”已经失去事实前提。必须先更新架构决策，以当前上游 Web 源码为基础实现 target adapter。把 Web 放在外部 Server OS 只能叫混合部署，不能作为“纯 Cloudflare target 完成”的证据。

### CF-F3：身份服务可用，但未实现两 target 共用的身份契约 v1

- Edge `workers/edge/auth.ts:9-61` 对 bearer 和 cookie 都经 Auth service binding 调 `/internal/verify`；`BETTER_AUTH_JWKS_URL/ISSUER/AUDIENCE` 在 `edge/env.ts:14-16` 仅声明，没有校验消费点。
- Auth `index.ts:215-224` JWT TTL 硬编码 `24h`，使用默认 issuer/audience；共享 `auth/shared/` 不存在，Firebase scrypt 验证/首登迁移还在 `workers/auth/firebase-migration-password.ts`。
- 本地真实签发结果：`exp-iat = 86400` 秒，`sub` 存在、`uid` claim 缺失，`iss`/`aud` 都是本次 Auth origin。与 `02-deployment-profile.md:65-70` 要求的 profile TTL 默认 3600、`sub=uid` 加独立 `uid`、显式 issuer/audience/JWKS 校验不一致。
- 现有验证确实执行 Better Auth 校验（`auth/index.ts:857-945`），不能写成“现在完全没验证 JWT”。需要修复的是统一的权威与配置契约，不是仅改 TTL 常量。

### CF-F4：WebSocket 首帧仍要求 CF 自己的票据

`workers/edge/index.ts:1118-1156` 创建 bootstrap、删除 Authorization，并提供 `/v1/realtime/web-ticket`。Realtime `session.ts:470-486` 的首帧读取 `auth.ticket` 并执行 `verifyRealtimeTicket`。统一目标要求沿用上游 `/v4/web/listen` 首帧 `{type:"auth", token:<JWT>}`。当前上游客户端不能只切 profile 就获得这套 CF 流程；必须在 Edge/Realtime/DO 的同一 PR 中闭合验证、超时、重放和断连行为，避免再让客户端长期保留 CF 特判。

### CF-F5：profile/品牌没有成为部署配置的唯一输入

`deploy/profiles/cloudflare.yaml` 已声明能力与品牌域占位符，但 Workers 没有读取这份配置的部署渲染入口。配置中的 service/D1/R2/Queue/Vectorize 名称仍为 `omi-cf-*`；staging 的 CORS、MCP、公开 URL 仍含 `summersmile1984.workers.dev`。生产子域默认值的修复已经存在，不等于完整白牌资源生成完成。

`validate-manifests.mjs:268-285,730-736` 甚至强制 `omi-cf-` 前缀；`scripts/production-environment.mjs:8-84`、`scripts/d1-migrations.mjs:7-35`、部署器和回滚快照仍各自拥有资源名称。`scripts/brand/apply.py:39-52` 已存在（旧 followups 中“B0 尚不存在”已过时），但 `--only` 没有 `cloudflare`，注册 generator 只有 Flutter。

只改 Worker 名称会断 service binding、迁移 authority、验收 URL 或回滚定位。必须一起渲染资源、绑定、域、迁移计划、秘密名称清单与发布/回滚引用。

### CF-F6：纯 CF、混合回源、能力不可用的契约尚未被共同验证

未配置 origin 时，Edge 未覆盖路由按当前逻辑返回 `{error:"route not migrated"}` 404（匿名可能 401），并非自动等价于上游 FastAPI 错误体。`index.ts:522-546,2417-2431` 的回源路径保留客户端 bearer，目标地址从 `ORIGIN_BACKEND_URL` 取；没有证据证明它能被本品牌 Server OS 的 JWT verifier 接受，也没有 owner/table-family 规则证明混合部署绝不会让同一业务族同时写 D1 和 PG。

纯 CF 的 AI 政策为 Workers AI；禁用的外部 AI 兼容路由在 Edge 有显式 503 分支（`:2380-2390`）。业务外部连接器并不等于 AI provider。当前 route inventory 的 `staging-owned` 是归属声明，不表示所有 UI 功能或外部 provider 已完成生产验收。`API Core ARCHITECTURE.md:10-15,23-28` 等仍声明若干搜索/自动化/推送边界未迁移。下一步应由静态 profile 表达可用能力、客户端按表消费，再以行为测试证明返回/入口与所声明能力一致。

### CF-F7：CI 尚未覆盖 target 运行闭环，工具版本也不能完整重放

`.github/checks-manifest.fork.yaml` 当前六项检查没有 CF manifest/routes/migrations 或真实 Workers contract runner；`deploy/cloudflare/ci/contract.sh` 不存在，`contracts/auth`、`contracts/realtime`、`contracts/api-smoke` 也未建立。不能只新增一个“validate:manifest 通过”步骤就宣布 C4 完成。

Wrangler/npm 依赖已锁，Python 两个 `pylock.toml` 也已提交并被 pywrangler 使用；但运行 `uv run --frozen pytest` 会因缺 `uv.lock` 失败，文档命令 `uv run pytest` 则新解析开发依赖。Node 没有本 target 的版本声明，本轮实际 Node 24.18.0 / npm 11.16.0，pytest 使用 Python 3.14.3，Worker 打包使用 pywrangler 管理的 Python 3.13 环境。这是环境复现缺口，不是本轮测试失败的证明。

## 4. 实际命令与验收账本

以下命令除标注外均在 `deploy/cloudflare/` 执行；每个 Worker 独立 dry-run，无资源创建。所有 exit 数字均来自本轮实际运行。

| 命令/动作 | exit/结果 | 日志 | 能证明 / 不能证明 |
|---|---|---|---|
| `npm ci` | 0，121 packages，约 5 分钟 | `npm-ci.log` | 锁定 npm 依赖可安装；未修改锁文件 |
| `npm run typecheck` | 0 | `typecheck.log` | TS 编译类型通过 |
| `npm test`（含 pretest） | 0；106 文件、775 tests | `tests.log` | Node 替身单元测试通过；不是实际 DO/Queue 联调 |
| `npm run validate:manifest` | 0；628 CF routes、577 backend inventory、26 resources、37 Redis families、7 vector、10 R2 namespaces | `manifest.log` | 静态清单一致性；实际主线 route completeness 未覆盖 |
| `npm run validate:backend-routes` | **2，invalid surface** | `routes.log` | 标准发布入口的确定性阻塞 |
| `backend/scripts/openapi_runner.sh /tmp/.../route-compare.py` | 0（诊断脚本）；612 actual / 577 inventory，缺36/旧1 | `route-compare.log` | 当前真实注册路由与旧清单差异；exit0仅指诊断执行完，不表示差异合格 |
| Core `uvx uv==0.12.3 run pytest -q` | 0；395 passed、1 deprecation warning | `api-core-pytest.log` | 主机 Python 测试；不是 Pyodide 生产端到端 |
| AI 同命令 | 0；118 passed | `api-ai-pytest.log` | 同上 |
| 两 Python 项目尝试 `uvx uv==0.12.3 run --frozen pytest -q` | 2，缺 `uv.lock` | `core-pytest.log`、`ai-pytest.log` | 后续按仓库正式命令成功；勿写成没有任何 Python 锁文件 |
| `npx --no-install wrangler deploy --dry-run --config workers/<name>/wrangler.jsonc`，name=auth/rate-limit/realtime/jobs/edge | 每个 0 | `dry-run-<name>.log` | TS 工件及配置可打包；没有品牌 production 渲染/资源/secret验收 |
| Core/AI `uvx uv==0.12.3 run pywrangler deploy --dry-run` | 每个 0 | `dry-run-api-core.log`、`dry-run-api-ai.log` | Python Worker 模块与 pylock 依赖闭包能打包 |
| `npm run verify:migrations` | 0；两 staging D1 均无 pending | `verify-migrations.log` | **远端只读**，脚本本身查的是现有 staging；不是 hermetic CI 检查，也未验证 production 迁移 |
| `wrangler d1 migrations apply AUTH_DB --local --config workers/auth/wrangler.jsonc --persist-to /tmp/.../local-state` | 0；10 SQL | `local-auth-migrations.log` | 本地空库初始化，不是历史库升级兼容/回滚演练 |
| 同命令 APP_DB / edge config | 0；155 SQL | `local-app-migrations.log` | 同上 |
| `wrangler dev --local --config workers/auth/wrangler.jsonc --port 18878 --inspector-port 19878 --persist-to /tmp/.../local-state` + 本地专用secret vars | 服务就绪；检查完成后主动停止，进程exit130 | `local-auth-runtime.log` | 真正 workerd、D1 Auth运行；secret为临时非生产值，日志仅显示hidden |
| `python3 /tmp/.../local-auth-probe.py` | 0；健康/注册/JWT/两种验证200，两个反向401 | `local-auth-probe.log` | 实际认证边界正反路径；无 token/密码输出；未证明客户端与其他 Worker 联动 |
| `cd web/app && npm run build:vinext:staging` | **1，Missing script** | `web-vinext.log` | 当前统一main Web不能沿现有CF脚本构建 |
| 公开 Edge `/ready`、Web `/api/worker-ready` GET | curl exit0、HTTP200 | `production-edge-ready.log`、`production-web-ready.log` | 已有线上实例健康；不能归属本次main |

本地fixture复现顺序：从空的`/tmp/.../local-state`应用两个数据库迁移，启动上表Auth配置，注入仅用于该临时环境的`BETTER_AUTH_SECRET`与`INTERNAL_ASSERTION_SECRET`（值见一次性`local-auth-probe.py`及审计命令，不是现有账户secret），执行探针后停止该进程。探针使用随机`@example.invalid`地址，只记录状态、claims形状和TTL；没有输出token/密码。它不依赖远端D1或真实账号。

总体 preflight 的证据由总审计记录在 `/tmp/memweft-audit-20260904/common/`：以 `origin/main` 为 base 时文件差异为 0，只选 11 上游 + 1 fork 检查且通过，历史 failure-class guard 因 shallow history skip；**这不证明 target 可部署**。累计基点 `fd01c27267` 对 898 个文件选 50 项，在第5项文件行数 ratchet 失败，当时剩余45项和fork阶段未执行；随后6个相关已有检查独立补跑均通过，仍有39项没有累计范围执行结果。它是历史变更聚合缺少原PR例外信息导致的 gate 状态，不是4个新增运行故障。详细归因以总审计为准。

本路最终验收判定：**组件级通过；当前 main 的整套部署不通过；同客户端双target不通过；纯CF白牌交付不通过。** 未执行全量 deploy/smoke 的原因是前置命令和客户端契约已有明确缺口，本轮任务是制作行动方案，未授权生产资源变更。已有 9 月 1 日独立分支上线记录只能证明那次发布，不能抵消本次失败证据。

## 5. 按依赖组织的行动包

共享 owner：`AUTH-1` 由 Server OS 路牵头服务端身份权威，CF 交付 D1/Workers adapter；`CLIENT-1` 由白牌路牵头四端 profile、登录/刷新、回调与实时客户端；`WEB-1` 由 CF 路牵头当前上游 Web 的双 target 打包，Server OS 路配合；`CI-1` 两 target 维护同一 `contracts/` 套件的各自 runner，白牌路在其上加品牌×平台矩阵。

**联合交付批次 `INTEGRATION-1`：`CLIENT-1` 的 Web 消费者、`CF-2` 与 `WEB-1` 在同一短期集成 PR 的候选树上开发和验收。** 前置是 WL-1 的明确 schema、AUTH-1 的身份契约与测试服务，以及 Server 路可执行的参考后端；三部分不互相等待“对方已合并”。可以分提交、分模块并行，但必须在同一候选版本跑通 Web 对两个 target 的登录与实时闭环后再合入。其余原生平台的 CLIENT-1 可随后按平台独立交付，各自仍要求两 target 完整通过。工作包不是强制一包一 PR，联合批次也不另开长期分支。

### CF-1（P0）：恢复当前主线的路由归属与可运行 preflight

- **范围/产物**：在 fork 目录实现 `scripts/route_inventory.py`，复用上游 hermetic bootstrap；替换 CF npm/deploy 调用，重新登记实际612路由（后续上游同步后以新枚举为准）；36缺项逐条确定 CF owner/受支持行为/显式不可用或同品牌origin归属，移除陈旧项。同步 README 和 `08-m2-cloudflare-followups.md`，接现有 fork manifest 的 local+ci lane。
- **核心测试**：真实生产路由枚举对清单；已有 HTTP/WS 和通配归属通过；新增上游路由自动变 unclassified 并使检查失败；重复/无owner/旧清单/错类型失败。静态检查与行为覆盖分开描述。
- **验收门禁**：干净 worktree `validate:backend-routes`、`validate:manifest` 均通过，且用故意新增路由夹具证明 gate 会拒绝漂移。该 guard 直接对应本次失效和既有 2026-08-29 API 404 事故。
- **依赖**：无，可立即执行。不能以禁用检查或编辑上游 exporter/锁文件完成。

### AUTH-1（P0，共享包）：一个签发/校验/刷新契约，两个存储adapter

- **范围/产物**：Server OS 路建立 `auth/shared/` 的 JWT参数、`sub+uid`、issuer/audience、TTL/轮换与 Firebase密码迁移纯逻辑；CF 同时迁移 Auth adapter、Edge bearer JWKS verifier 和 cookie-only内部verify。两端原有调用迁完，禁止留下两份常量权威。
- **核心测试**：两个真实运行环境均注册→登录→token→同claims校验→刷新；错误issuer/audience/签名、过期、未知/轮换kid、JWKS不可用、撤销/删除后的行为、旧Firebase主体和新品牌空账号分别覆盖。必须明确本地JWKS验证下的联合权威模型，保留适用的撤销、sessionGeneration、账户删除与迁移准入语义，并对每项建立等价拒绝用例，不能为了减少一次service call丢掉原本权威判定。
- **验收门禁**：同一 `contracts/auth` 对两个target通过；JWT实测默认3600、显式iss/aud、`sub=uid`；cookie不暴露到公共跨域回源，服务内部断言绑定method/path/audience的现有拒绝测试继续通过。
- **依赖**：服务端profile身份字段由两路共同定稿；与 `CLIENT-1` 协作上线但不把客户端兼容分支作为最终完成标准。

### CF-2（P0）：以现有上游首帧协议完成真实实时会话

- **范围/产物**：Edge、Realtime路由、DO会话与相关测试作为一个可验收表面，统一 `/v4/web/listen` 的真实JWT首帧；移除公开web-ticket/session额外流程及客户端依赖，内部HMAC机制仅作Worker边界实现。记录协议夹具到 `contracts/realtime`。
- **核心测试**：相同Web客户端/profile切换后建立会话、发送首帧并得到auth响应；未认证音频、超时、过期/错aud/JWT、重放/重复连接、上游ASR失败、断连清理均有行为用例；不得用源码字符串顺序替代DO执行。
- **验收门禁**：workerd真实WebSocket和DO路径通过；两个target接受同一首帧夹具；需要真实ASR的验收在明确目标环境运行，保留受控数据与清理证据。
- **依赖**：AUTH-1 的身份契约/测试服务、WL-1 的profile输入；与 CLIENT-1 Web 消费者和 WEB-1 同属 INTEGRATION-1，前置不是对方最终交付已合并。可先建立协议夹具，不先另发“改header常量”PR。

### WEB-1（P0）：当前上游Web源码的双target可部署工件

- **范围/产物**：先以当前 Moonshine/Bun 源码做有边界的构建/运行验证，给出所依赖的Bun、文件系统、服务端fetch/SSR接口清单和Workers adapter可行性结论；在fork路径落Cloudflare运行/打包adapter与Server OS构建入口，更新失效D3和所有资格/发布命令。沿用上游源码，fork依赖独立拥有，不退回旧Next长期分支。
- **核心测试**：同一源码两个工件都能登录、同源cookie代理、JWT换取、API错误透传、WebSocket录音；直接访问路由和静态资源加载；错误Auth配置/服务binding缺失/构建输出缺文件立即失败；第三方OAuth回调如启用则验证完整origin与cookie边界。
- **验收门禁**：目标本地运行 + staging真实浏览器均完成核心页面；Cloudflare Web工件直接跑Workers并通过readiness；无 `next typegen` 或不存在的vinext脚本作为生产必需步骤；Server OS工件也运行相同Web源码。
- **依赖**：可行性检查可与CF-1/AUTH-1并行；AUTH-1和WL-1提供稳定输入，与CLIENT-1 Web/CF-2在INTEGRATION-1同一候选树联合验证。品牌域可先用隔离测试域，正式发布再验证实际运营域。若adapter仍未实现，保持纯CF Web项未完成，不能以外置Bun服务或只发布API替代用户目标。

### CF-3（P1）：品牌×target配置及资源完整渲染

- **范围/产物**：扩展白牌系统的CF generator（可称B9），由brand/profile/stage生成Worker/service/D1/R2/Queue/Vectorize、域/CORS/MCP、迁移authority、secret名称与发布/回滚引用。现有线上`omi-cf-*`仅为既有品牌实例，不能作为通用默认。
- **核心测试**：固定的`omi-upstream × target × stage`回归夹具输出一致，不把个人staging配置当通用默认；两个测试品牌输出互不相交；同品牌不同stage隔离；缺域/ID/secret映射失败；绑定依赖闭包、迁移目录和回滚引用均来自同一渲染结果；重复渲染幂等。
- **验收门禁**：两个品牌渲染包均能validate + 七Worker/Web dry-run；确认输出不含其他品牌域/ID；已有资源不自动改名迁移，不触发数据删除。生产资源命名/迁移需明确发布审批。
- **依赖**：白牌路品牌schema、CLIENT-1 profile生成；CF-1、WEB-1提供需要渲染的真实工件边界。

### CF-4（P1）：纯CF能力与可选同品牌origin的行为契约

- **范围/产物**：对新增/现有路由建立状态族owner映射，纯CF已支持能力、显式未支持能力与可选origin路由分开声明；修正与上游不一致的错误形状；通过profile传静态能力，客户端不得获知DO/HMAC或迁移围栏细节。
- **核心测试**：无origin时支持路径闭环、未支持路径按契约返回且UI隐藏；配置origin后同JWT被参考Server OS接受、请求目标和数据owner正确；跨品牌域、错误issuer、origin超时/错误、D1+PG双写企图均拒绝；触发回退必须保留共享fallback遥测。
- **验收门禁**：纯CF在无origin且无旧Omi/GCP/外部AI依赖条件下完成声明支持的主流程；混合模式独立通过owner规则和出站观测。不要将`0 legacy-owned`清单数字当作功能验收。
- **依赖**：CF-1、AUTH-1、CLIENT-1；混合验证依赖标准服务器参考实现可运行。

### CI-1（P1，共享包）：同一核心流程在两个真实target持续验证

- **范围/产物**：建立共用 `contracts/auth`、`contracts/realtime`、`contracts/api-smoke`；CF runner启动实际workerd/本地D1迁移/DO/必要Worker，不以9行stub替代；Node/uv/Python开发与运行依赖可重放。把本地migration验收与远端只读migration状态检查区分，后者不能直接放进hermetic CI。
- **核心测试**：登录→录音/上传→对话→记忆→导出成功；主故障用例至少覆盖无效身份、provider失效、异步重复/重试、存储失败或删除围栏；迁移空库、已有schema升级、重入幂等及对应回滚版本兼容。CI用受控provider seam，不访问真实外部服务。
- **验收门禁**：两target以同夹具运行，任一target变化均跑本路套件；local+ci在现有manifest均注册；失败保留trace与部署/品牌profile；upstream-mode与fork-mode分离，现有上游测试不修改。
- **依赖**：CF-1后可先补基础runner；最终闭环依赖AUTH-1、CF-2、WEB-1、CF-4；白牌路再加品牌×平台构建矩阵。

### CF-5（P2）：带版本证据的完整发布与部署验收

- **范围/产物**：发布先完成品牌配置解析、所有测试、Web+七Worker全部构建和dry-run，再执行资源/迁移/服务部署；生成包含Git SHA、brand、target、stage、依赖锁hash、迁移清单、Worker versions、Web工件hash的release record。保留依赖部署顺序、回滚快照和D1不随Worker rollback回退的明确边界。
- **核心测试**：坏Web构建、坏路由清单或缺配置必须在首个外部写入前失败；注入中途Worker发布/健康/烟测失败时恢复允许恢复的版本；历史schema与回滚Worker兼容，不能假装migration被回滚。
- **验收门禁**：授权的独立staging从当前main重建部署；用专用空测试身份和真实终端完成登录→录音→对话→记忆→导出→删除/残留检查，浏览器错误与出站依赖可追踪。release record明确绑定本次源码，才允许将线上200作为该版本验收。生产发布、schema/资源迁移与CI发布流程变更按仓库显式签署规则执行。
- **依赖**：CF-1至CF-4、AUTH-1、WEB-1、CI-1和白牌客户端工件。现有9月1日记录仅可用作历史对照。

## 6. 执行顺序与停止条件

第一批并行做 **CF-1、AUTH-1、WEB-1可行性/构建契约**，白牌路同步完成WL-1并准备CLIENT-1。第二批以 **INTEGRATION-1** 联合交付Web消费者、CF-2和WEB-1；CF-3、CF-4和CI-1的target runner使用同一身份、profile和协议夹具推进，原生CLIENT-1随后按平台完成。第三批在CI-1闭环后执行CF-5的当前main staging发布和真实终端验收。

独立包或明确联合的INTEGRATION-1批次，只在其完整可验收表面通过后合入，提交附本轮失败实例、核心/错误测试、`scripts/pr-preflight --suggest`与failure-class判定；按普通PR合入唯一main。整路线的完成条件是当前main对同一白牌客户端/profile可复现完整Cloudflare工件和真实主流程，且证据绑定该SHA；“测试绿”“旧线上健康”“某个Worker能打包”都只是其中一层。
