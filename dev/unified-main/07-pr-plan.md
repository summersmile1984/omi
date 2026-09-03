# 07 · PR 计划、决策登记与排期

> 规模假设：2 名工程师（一人偏客户端/Swift/Flutter，一人偏后端/CI/Workers），品牌与合规事项由产品/法务并行推进。工时为实现 + 自测 + PR 评审，单位为人日。所有 PR 目标分支 `main`，regular merge，不 squash。

## 1. 决策登记（开工前签字，写进 `dev/unified-main/decisions.md`）

| ID | 决策 | 推荐 | 影响的 PR |
|---|---|---|---|
| D1 | profile 两轴：`deployment_target ∈ {omi_cloud, self_hosted, cloudflare}` + `stage ∈ {production, beta, local}`；身份提供方派生 | 采纳 | S1–S5 |
| D2 | **契约权威 = 上游 API + 自托管参考实现；Cloudflare 单向对齐。** 身份契约 v1：路径 `/api/auth`；JWKS 本地校验；JWT TTL 可配（默认 3600s）；移动端真实 Better Auth 流程；Web cookie + 同源代理；实时沿用上游 `/v4/web/listen` 首帧 token（不引入 web-ticket、不新增上游没有的端点） | 采纳 | S2, S4, S5, S6, M2 |
| D3 | Web 运行时：上游 Next.js 源唯一、与上游同步；自托管用上游 `web/app/Dockerfile`（Node standalone）；Cloudflare 用 vinext；**不引入 Bun**，Moonshine 归档不合入 | 采纳 | S4, M1 |
| D4 | Cloudflare 未迁移路由：支持 `ORIGIN_BACKEND_URL` 指向自托管后端（混合）；未配置则返回上游同形 404 + 客户端静态能力表（不新增上游没有的端点） | 采纳（混合为可选） | M2 |
| D5 | 向量：自托管 Qdrant 任意维；Cloudflare Vectorize ≤1536 → `embedding_dims` 进 profile，模型按目标选择 | 采纳 | S1, M2 |
| D6 | 品牌与 BLE UUID 分离、MCUboot 换密钥（白牌 Phase 0） | 采纳 | B7 |
| D7 | 推送：两目标默认 `webhook`（或品牌自有 FCM 项目）；禁止上游 Firebase 项目 | 采纳 | S5 |
| D8 | 账户激活围栏（409）新品牌默认关闭 | 采纳 | M2 |
| D9 | 首发关闭应用市场/Personas 入口 | 采纳 | B4 |
| D10 | 上游文件"禁改清单"（T2）成为 fork 纪律并由 `fork-upstream-touch` 守卫 | 采纳 | C1 |
| D11 | **上游文件零改动为默认（T0）**；T1 白名单单点 ≤3 行且附上游 PR，只减不增；`backend/**` 与上游测试/锁文件/生成文件/CI 为 0 条 | 采纳 | 全部 |

## 2. PR 一览

### S · 接缝（先行）

| PR | 标题 | 依赖 | 人日 | 验收证据（写进 PR 描述） |
|---|---|---|---|---|
| S0 | sync: upstream/main 2026-09 + 消除 13 个冲突源 | — | 1 | `merge-tree` 冲突数 0；`make preflight` 绿 |
| S1 | profiles: 单一事实源 + render/check + 契约夹具 | S0 | 3 | `render.py --target omi_cloud` 与现有字面量一致；`check_tables.py` 绿 |
| S2 | app: 统一 deployment profile + Better Auth 客户端 + Firebase 包级 shim（`pubspec_overrides.yaml`，调用点零改动） | S1 | 6 | 上游模式 `app/test.sh` 绿；fork 模式 `env_test` 迁入 `app/test/fork/` 后绿；三种 `OMI_APP_PROFILE` 构建登录闭环 |
| S3 | desktop: Windows/macOS profile 增加 cloudflare + 统一配置模块 + 日历 Worker 门控 | S1 | 4 | 两端 profile 测试绿；命名 bundle 连 `wrangler dev` 登录 |
| S4 | web: profile 对象 + 同源 Better Auth 代理 + vinext 加法（Next 源与上游同步；`next.config.js` 加法为 T1 白名单项并提上游 PR；实时沿用上游首帧 token） | S1 | 4 | `web-checks` 绿；`build:vinext:staging` 绿；Playwright 登录用例双运行时通过 |
| S5 | backend: `backend/fork/` 包 + `fork/main.py` 入口 + 导入时补丁注册表（identity/push/storage/queue/egress/STT/TTS/翻译 provider），上游后端文件恢复零改动 | S1 | 6 | 上游模式 `backend/test.sh` 绿；`backend/fork/tests/` 在 `self_hosted` 模式绿；`fork-upstream-touch` 对 `backend/**` = 0；补丁自检通过 |
| S6 | auth: `auth/shared/` 共享包 + auth-server adapter | S5 | 3 | `contracts/auth/` 对 auth-server 全绿 |
| S7 | tests: 两条测试通道（上游模式 / fork 模式）+ fork 测试目录 + 上游测试零改动守卫 | S5 | 2 | 上游组件测试在无 shim env 下绿；fork 目录被各 runner 发现（`backend-test-discovery`） |

### M · 合并

| PR | 标题 | 依赖 | 人日 | 验收证据 |
|---|---|---|---|---|
| M1 | merge: self-host 新增目录（deploy/self-host、firestore_pg 余量、STT 管线、fork 不变量） | S5, S6 | 2 | `fork-contract-selfhost` 绿；`operations.sh self-check` 通过 |
| M1-win | merge: Windows 自托管功能（模型能力 IPC、egress 边界）按 profile 重写 | S3, M1 | 3 | Windows CI 绿；自托管 profile 手测 |
| M1-mac | merge: macOS Better Auth 登录与自托管功能按契约 v1 重写 | S3, S6 | 3 | macOS 测试绿；`omi-*` 命名 bundle 对自托管栈登录 |
| M1-ctx | merge: context-for-claude 自托管适配 | M1-mac | 1 | 其自带测试绿 |
| M2 | merge: deploy/cloudflare + docs + CF 契约对齐（basePath、JWKS、TTL、围栏、资源名品牌化、ORIGIN_BACKEND_URL） | S2–S6 | 6 | `fork-contract-cloudflare` 绿；104 TS + 70 Python 测试绿；Flutter `cloudflare.local` 闭环 |
| M3 | ci: C0–C6 落地（见下） | M1, M2 | — | 两目标矩阵在 main 全绿一周 |
| M4 | chore: 删除长期分支 + AGENTS.fork.md 规则 + 旧计划文档加取代说明 | M3 | 0.5 | 分支列表只剩 main 与短期分支 |

### C · CI（可与 M 并行）

| PR | 标题 | 依赖 | 人日 | 验收证据 |
|---|---|---|---|---|
| C0 | ci: 禁用上游部署/机器人工作流（`gh workflow disable` 脚本）+ `remote.upstream.tagOpt --no-tags` | — | 0.5 | Actions 页面只剩保留列表 |
| C1 | ci: `checks-manifest.fork.yaml` + `scripts/fork/preflight` + `fork-checks.yml` + `Makefile.fork` + `fork-upstream-touch` | S0 | 2 | PR 上两条 hygiene 都绿 |
| C2 | ci: `deploy/matrix.json` + `fork-build-matrix.yml` + secret gate action | C1, S1 | 2 | 无密钥 PR 全绿；有密钥产出可安装件 |
| C3 | ci: `fork-contract-selfhost.yml` + `compose.ci.yml` + `ci/contract.sh` | M1 | 2 | 契约套件对自托管栈通过 |
| C4 | ci: `fork-contract-cloudflare.yml` + `ci/contract.sh` | M2 | 2 | 同一契约套件对 `wrangler dev` 通过 |
| C5 | ci: 四条发布工作流（selfhost / cloudflare / macos / firmware） | C2–C4, B7 | 4 | 各在 staging 完成一次真实发布并留证据 |
| C6 | ci: `fork-upstream-sync.yml` + PR 模板 + `sync-log.md` | C1 | 1 | 首次自动同步 PR 生成并合并 |
| C7 | upstream: `run_checks.py` 多清单 / `check_agent_doc_references --extra` / `check_deployment_secret_boundary --extra`（回推上游） | C1 | 1 | 上游 PR 开出；接受后删除 fork 绕行 |

### B · 白牌层（可与 M/C 并行，详见 `04-brand-layer.md` §4）

| PR | 标题 | 依赖 | 人日 |
|---|---|---|---|
| B0 | brand: 骨架 + schema + `omi-upstream` 清单 + apply/check 空实现 + 词表 + fork 清单登记 | C1 | 2 |
| B1 | desktop: 拆硬门槛（app-config.sh、AppBuild、Info.plist 模板、BundleEnvironment、desktop_previews） | B0, S3 | 3 |
| B2 | app: 身份注入点（flavorizr 生成、flavors.dart、pbxproj、entitlements、Info.plist、profile 表接品牌域名） | B0, S2 | 3 |
| B3 | app: 品牌文案（运行时 `LocalizationsDelegate` 委托覆盖 190 个 ARB 键，不改上游 ARB；同时提上游 `{appName}` 参数化 PR）+ 556 处硬编码中用户可见部分 + 字体替换 | B2 | 6 |
| B4 | backend: `backend/fork/brand.py` + `fork/patches/brand.py`（导入时替换上游 prompt 常量，上游文件零改动；同时提 `{product_name}` 参数化 PR）+ 通知/模板/OpenAPI/前缀 + share_links + firmware 映射 + 制品名 | B0, S5 | 5 |
| B5 | desktop: 49 文件文案 + 9 prompt + 反域名集中 + 资源替换 + Windows electron-builder | B1 | 5 |
| B6 | web/docs: 四站点 + public-build-values 生成 + docs.json + Dockerfile.datadog | B0, S4 | 3 |
| B7 | firmware: brand.conf + nfc + UUID 基址 + 新 MCUboot 密钥 + 删除预编译件 + 发布工作流 | B0, D6 | 3 |
| B8 | governance: INV-UI-1/INV-BETA-1 参数化 + impeccable/design-qa fork 版 + 不改清单进 AGENTS.fork.md | B1–B7 | 2 |

## 3. 排期（10 周，两人并行；周为工作周）

| 周 | 工程师 A（客户端） | 工程师 B（后端/CI） | 产品/法务（并行） |
|---|---|---|---|
| 1 | S0 协作解冲突；S1 | S0；C0、C1 | D1–D11 签字；品牌名商标检索；开发者账号申请 |
| 2 | S2 | S5、C2 | 域名、Apple/Google/Cloudflare/注册表账号 |
| 3 | S3 | S6、M1 | 模型供应商与备案路径确定 |
| 4 | S4 | M2（前半：目录合入 + basePath/JWKS） | 法律文本、隐私政策草稿 |
| 5 | M1-mac、M1-win | M2（后半：围栏/资源名/ORIGIN）、C3 | 商店素材设计 |
| 6 | M1-ctx、B1、B2 | C4、B0、B4（前半） | Bluetooth SIG 会员/Declaration 立项 |
| 7 | B3（前半）、B5（前半） | B4（后半）、B6 | 固件签名密钥托管方案（HSM/secret） |
| 8 | B3（后半）、B5（后半） | B7、C5（selfhost/cloudflare） | 认证实验室排期（若做硬件） |
| 9 | B8、C5（macos/firmware） | C6、C7、M3 观察期 | 商店上架提交 |
| 10 | 缓冲、全链路 E2E、M4 | 缓冲、同步演练、sync-log | 上线检查单 |

关键里程碑：**W3 末** `main` 以 `self_hosted` profile 端到端可用；**W5 末** 两目标契约套件在 `main` 全绿、长期分支冻结；**W8 末** 白牌构建零泄漏；**W10 末** 一次上游同步在半天内完成且两目标发布工作流各完成一次 staging 发布。

## 4. 每个 PR 的通用模板（贴进描述）

```
## 范围
- 属于：S/M/C/B-<n>，依赖：<PR ids>
- 上游文件改动：<n> 个（目标 0；非 0 时逐条列出所属 T1 白名单条目与行数，粘贴 fork-upstream-touch 输出）

## 验证
- 命令与结果（make preflight / scripts/fork/preflight / 组件 test.sh / 契约套件）
- 手工路径：<profile> × <brand> 构建 → 登录 → 录音 → 对话 → 导出（截图或日志片段）

## 度量
- 合并后 `merge-tree` 对 upstream/main 真实冲突数：<n>（上一次：<m>）
- 上游文件被 fork 触碰总数：<n>（上一次：<m>）

## 不变量
- 命中的 INV-* 与守卫测试改动说明；Failure-Class 声明（fix: 类）
```

## 5. 风险与应对（执行期）

| 风险 | 触发信号 | 应对 |
|---|---|---|
| 接缝重写耗时超预期 | S2/S5 超过 1.5 倍工时 | 先只支持 `self_hosted` + `omi_cloud`，`cloudflare` 值在 M2 再加；不阻塞 M1 |
| 上游在 S 系列期间大改客户端认证 | 周同步冲突落在 `auth_service.dart` | 以上游为准重套 profile；把 profile 抽象尽早提 PR 给上游 |
| Cloudflare 契约对齐（JWKS/basePath）破坏其现有生产 | `smoke:production` 失败 | CF 生产是独立 `workers.dev` 环境且无真实用户；接受一次性重建（D1 迁移已幂等） |
| 自托管 Web 资源占用 | 4C8G 上 Next standalone 内存偏高 | 先量测；必要时 `output: 'export'` 静态化非 SSR 页面或把 Web 放到独立小机；不换运行时 |
| 导入时补丁随上游重构失效 | 上游重命名了被补丁的符号 | 补丁注册表启动自检（符号缺失即失败）+ 周同步 CI 的 fork 测试立刻暴露；修补丁而不是改上游文件 |
| 白牌 B3 文案量大 | 49 语言复核滞后 | 先 zh/en 两语言零泄漏上线，其余语言用占位符机械替换 + 逐语言复核清单 |
| 两位工程师都在解同一冲突 | 同一周两个 PR 触及 `checks-manifest.fork.yaml` | fork 清单按目录拆条目、每 PR 只加自己的 id；周一合并顺序在 standup 定 |
