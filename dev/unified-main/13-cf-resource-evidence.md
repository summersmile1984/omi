# CF3：品牌、stage 与 Cloudflare 资源归属

日期：2026-09-04。实现基准为整合树
`a1f4ae7733b8bcf96bcd16d96bf80ee5df6c950e`；短期分支
`codex/implement-cloudflare-resources`，工作树
`/Users/macstudio/Documents/memweft-worktrees/implement-cloudflare-resources`。
本包只实现资源输入、派生配置、迁移及回滚引用契约，没有远端写操作。

## 交付与边界

- [资源输入](../../deploy/cloudflare/scripts/resource-input.mjs)：品牌/target/stage
  一致性、显式账号和 D1 ID、26 项物理资源命名、基础 secret **名称**映射及跨计划冲突检查。
- [配置派生](../../deploy/cloudflare/scripts/resource-configs.mjs)：读取当前七个 Worker
  模板和真实 Moonshine Web 工件；由实际 bindings 建立 28 项资源和部署依赖闭包。
  Auth issuer/audience、MCP issuer/resource、CORS 消费同一个实际 rendered profile。
- [物化及回滚契约](../../deploy/cloudflare/scripts/resource-bundle.mjs)：八份 Worker 配置、
  两份迁移配置、计划和回滚声明；显式观测的上一版本绑定到本计划。保留 D1，不猜测删除或 schema 回退。
- [正式入口](../../deploy/cloudflare/scripts/render-resources.mjs) 注册为 `npm run resources`；
  [SQL 验证器](../../deploy/cloudflare/scripts/verify-resource-migrations.py) 是实际 CLI 与
  Vitest 共用的生产原语。旧发布器尚未消费该计划，CF-5 仍须迁入当前 Web 构建和发布事务。
- [操作说明与完整输入示例](../../deploy/cloudflare/resources.md)；CF-4 账本新增 mount path
  契约。资格检查拒绝 API/MCP/share/object 前缀路径，不静默裁剪，不修改 Edge 路由。

`release_ready=false`、`remote_state_verified=false` 始终保留。渲染结果只报告
`render_valid`，不会把 dry-run、SQL 夹具或本地 D1 当成远端发布成功。

## 本地命令与结果

全部日志和合成夹具位于 `/tmp/memweft-implementation/cloudflare/resources/`。
以下 `${brand}` 为 `alpha`、`bravo`，`${role}` 为八个逻辑 Worker owner。
命令均从实现工作树运行；`npm run python` 通过已安装且经入口验证的
workers-py 1.16.7，未修改 npm、Python 或 Web 锁文件。

| 验证 | 命令 / 日志 | 结果及覆盖 |
| --- | --- | --- |
| Python 依赖闭包 | `UV_OFFLINE=1 CLOUDFLARE_PYWRANGLER_EXECUTABLE=<existing-1.16.7> npm --prefix deploy/cloudflare run python -- api-core sync`，AI 同形；`core-sync.log`、`ai-sync.log` | 均 exit 0；各自 committed pylock 的 10 项需求；新工作树生成本地 vendor 目录 |
| 当前 Web 实际构建 | `bun deploy/web/build.ts --target cloudflare --stage beta --manifest /tmp/memweft-implementation/cloudflare/resources/cf-${brand}.json --output /tmp/memweft-implementation/cloudflare/resources/${brand}-web`；`${brand}-web-build.log` | 两品牌各 exit 0、26 个路由；同源 SHA；此次 CF3 未改变 Web 源码 |
| 真实渲染与交叉归属 | `node deploy/cloudflare/scripts/render-resources.mjs --manifest …/cf-${brand}.json --inventory …/cf-${brand}-inventory.json --web-build …/${brand}-web --output …/${brand}-plan`，Bravo 另带 `--compare …/alpha-plan/resource-plan.json`；`${brand}-render.log` | 两品牌 exit 0；每品牌 28 项资源、12 个 JSON 和 2 个 Python 模块链接；无跨品牌资源/ID/origin/secret-ref 碰撞 |
| 渲染重复执行 | 同上带 `--check`；`${brand}-check.log` | 两品牌 exit 0、changed=[]；只比较不写入 |
| 五个 TS Worker + Web | `node deploy/cloudflare/node_modules/wrangler/bin/wrangler.js deploy --dry-run --config …/${brand}-plan/workers/${role}/wrangler.json`；`${brand}-${role}-dryrun.log` | Auth、RateLimit、Realtime、Jobs、Edge、Web 两品牌共 12 次 exit 0；实际打包及 bindings schema 验证，无上传 |
| 两个 Python Worker | `UV_OFFLINE=1 CLOUDFLARE_PYWRANGLER_EXECUTABLE=<existing-1.16.7> npm --prefix deploy/cloudflare run python -- ${role} deploy --dry-run --config …/${brand}-plan/workers/${role}/wrangler.json`；`${brand}-api-{core,ai}-dryrun.log` | 两品牌共 4 次 exit 0；保留锁定 Wrangler 4.127.0 / workerd 1.20260826.1 和 native pywrangler 版本检查 |
| 真正本地 D1 apply | `node deploy/cloudflare/node_modules/wrangler/bin/wrangler.js d1 migrations apply AUTH_DB --local --config …/${brand}-plan/migrations/auth.json --persist-to …/local-state`，App 用 APP_DB/app.json；`${brand}-{auth,app}-migrate.log` | 四个独立本地库全部 exit 0；每品牌 Auth 10 项 + App 156 项 SQL |
| 真正本地 D1 reentry | 重复上述四命令；`${brand}-{auth,app}-reentry.log` | 全部 exit 0；`No migrations to apply` |
| 品牌间持久数据隔离 | 用 Alpha 的生成 App 配置插入一个合成 task，再用两份 App 配置查询相同 uid；`alpha-isolation-write.json`、`${brand}-isolation-read.json` | 命令全部 exit 0；Alpha count=1、Bravo count=0；同一 persist-to 下由不同 D1 ID 实际隔离 |
| 资源生产契约测试 | `npm --prefix deploy/cloudflare test -- --run tests/resource-plan.test.mjs`；`unit-profile.log` | 12 tests；真实 profile renderer 的 local/beta/production 投影；正反资源/迁移/回滚/输出归属覆盖 |
| TS 完整组件 | `UV_OFFLINE=1 npm --prefix deploy/cloudflare test`；`component-test.log` | exit 0；108 files / 810 tests。现有静态清单仍报告旧 staging catalog 26 项；新完整派生计划的 28 项另含 Web/realtime DO，不是把旧清单测成了新清单 |
| 类型 | `npm --prefix deploy/cloudflare run typecheck`；`typecheck.log` | exit 0 |
| Python Core / AI | 各自目录 `UV_OFFLINE=1 uvx uv==0.12.3 run pytest -q`；`core-test.log`、`ai-test.log` | exit 0；Core 398、AI 118；这是 CPython 单元套件，不能替代 Python Workers runtime 证据 |

已有工具 override 的具体位置为
`/Users/macstudio/Documents/memweft-worktrees/cloudflare-adaptation/deploy/cloudflare/python/api-core/.venv/bin/pywrangler`。
fresh registry install/TLS/cache 边界与 [Python 入口验收](12-cf-runtime-evidence.md) 相同，
本包没有通过更改上游或锁文件规避。测试日志中的 early `unit-first.log` / `unit-sql.log`
分别是 10 / 11 项开发中结果，最终以 `unit-profile.log` 的 12 项及完整套件为准。

## 可复现合成夹具

品牌输入由 `brand/omi-upstream/manifest.yaml` 解析后生成临时 JSON：
brand id/display/short name/url scheme 替换为 `cf-alpha` 或 `cf-bravo`，domain 改为
自己的 `.invalid`，显式写入 `deployments.cloudflare.beta` 和 production 六个 root origins。
只评估资源和 Web profile，本次没有资格宣称这些克隆夹具已完成四端品牌资产/分发标识替换。
inventory 从 checked-in 示例生成：Alpha 使用序号 1 的合成 UUID；Bravo 使用序号 2；
品牌和所有 secret ref 前缀随身份隔离。账号为同一 32 位合成账号以加强同账号冲突测试。

依次执行 sync → 两个 Web build → render/compare/check → 八工件 dry-run →
两个 authority 的 local apply/reentry。D1 隔离行插入 `cf_action_items`，字段为
`uid='resource-isolation-fixture'`、`id='resource-isolation-task'`、合成 description、
`status='active'`、created_at/updated_at=1；只在 Alpha 插入，再分别 SELECT count(*)。
没有读取客户数据或使用远端部署权限。

SQL 夹具执行全部文件，并在最后一项迁移前建立旧 user/session 和 task，验证最后迁移后
同一主键/UID/状态可读且不丢失。夹具自己的 hash ledger 重入为零；实际 Wrangler 重入
另外由四个 local reentry 命令证明。它仍未运行任意历史 Worker binary，不能证明旧版本
代码对新 schema 的完整读写兼容；回滚产物显式要求 CF-5 补齐该证据。

## 未证明项与后续 owner

- CF-5：账号内真实资源身份、可用权限、secret 值、custom-domain/workers.dev 实际路由、
  发布/失败恢复事务、旧版本 Worker/schema 兼容，以及旧 Next/vinext 发布入口迁入。
- CF-4：36 项当前 route owner 工作、前缀路径、可选模型/支付/OAuth 等 provider 契约；
  基础 secret name mapping 不是所有 provider 配置完整性的结论。
- CI-1：同产品契约的双 target 全量矩阵；先前 Tasks 局部闭环继续有效，但相同空 POST
  请求仍为 CF 400 / Server 422，不能据 happy path 宣称完整 HTTP 语义一致。
- CLIENT-1 / 白牌 owner：四端真实终端与资产/分发闭环；本包仅证明资源/Web 构建维度可隔离。
- 无远端创建/改名/删除、无生产部署、无 push/PR/merge；旧本地 Auth/Edge/Core/Realtime
  联调工作树与服务保持原样。
