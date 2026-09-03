# 00 · 上游文件零改动策略（fork 纪律第一条）

> 原则：**能不改上游代码就不改。** fork 的全部行为差异通过新文件、包/模块别名、入口封装、导入时补丁、构建期生成文件与环境变量实现；确实做不到的进白名单，单点、≤3 行、附上游 PR 链接，白名单只减不增。
> 为什么值得这么严：这个 fork 每周合并上游。每一个被 fork 修改过的上游文件，都是未来某一周的冲突候选；而每一个新文件、别名或补丁，上游永远不会碰。

## 1. 诊断：`feature/cloud-neutral-shim` 为什么改了 653 个上游文件

| 类别 | 文件数 | 发生了什么 | 可避免？ | 本方案处置 |
|---|---|---|---|---|
| 上游测试 | **164**（`backend/tests` 86、`app/test`、macOS/Windows 测试） | 为让 shim 行为通过而改上游断言 | 是 | 上游测试在"上游模式"（无 shim 环境变量）原样运行；shim/profile 行为写在 fork 测试目录（见 §5） |
| l10n | **99**（49 ARB + 50 生成 `.dart`） | 加了自托管文案键后重新生成 | 生成文件：是；ARB：大部分 | 生成文件差异不提交、CI 生成；ARB 只保留自托管**必须**的新键并用 fork 前缀；品牌词不改 ARB（运行时委托或上游参数化 PR） |
| Windows 客户端围栏 | **~130** | 提交 `15cc5f19d0`（128 文件）、`dc2d74fc62`（76 文件）在每个调用点加 Firebase/Google/供应商出站围栏 | 是 | `vite.fork.config.ts`（extend 上游 vite 配置）用 `resolve.alias` 把 `firebase/*`、Google SDK 指向 `fork/packages/*-shim`；调用点零改动 |
| 后端 provider 内联 | `tts_provider.py` +345、`prerecorded_stt.py` +182、`routers/tts.py` +153、`storage.py` +199、`stt/streaming.py` +75、`cloud_tasks.py` +49、`stt_provider_policy.py` +34、`endpoints.py` +34、`main.py` +30、`_client.py` +26 | MiMo/MOSS/SenseVoice/TTS/MinIO/Redis 队列的实现与分发直接写进上游文件 | 是 | 实现迁入 `backend/fork/**`，由 `backend/fork/main.py` 入口的补丁注册表在导入时挂入；上游文件恢复原样 |
| macOS 围栏与登录 | 68 | 同 Windows，加 Better Auth 登录 | 部分 | Swift 无运行时补丁：生成文件（`Sources/Generated/`，上游已把该目录排除在格式化外）+ Info.plist 键 + 少数 T1 钩子 |
| context-for-claude | 22 | 姊妹应用的自托管适配 | 部分 | 同 macOS 做法 |
| 纯格式化 | 19 | 工具版本与上游不一致 | 是 | 禁止；工具版本钉住上游 |
| 其余（`backend/utils`、`routers`、`database`、`llm_gateway` 零散） | ~40 | 注入点与小修 | 是 | 按 §3 技术目录归 T0；真修 bug 直接提上游 |

同一时期 `codex/cloudflare-adaptation` 只改了 34 个上游文件，因为它把整个实现放在 `deploy/cloudflare/` 且不 import 上游后端——这就是目标形态。

## 2. 三级规则

| 级别 | 定义 | 例子 | 守卫 |
|---|---|---|---|
| **T0 零改动（默认）** | 上游文件一个字节不变；fork 行为来自新文件/别名/入口/补丁/生成物/环境变量 | `backend/fork/main.py`、`app/pubspec_overrides.yaml`、`vite.fork.config.ts`、`deploy/**`、`brand/**`、`*.fork.md`、`checks-manifest.fork.yaml`、`fork-*.yml` | `check-upstream-touch.py` 默认零 |
| **T1 白名单钩子** | 上游文件里单点、≤3 行、只做"读配置/调用 fork 钩子"，附理由与上游 PR 链接；白名单只减不增 | `AppBuild.swift` 的生产族 bundle id 改读 Info.plist；`next.config.js` 的条件别名；`main.dart` 的 `localizationsDelegates` 包装一行；`app-config.sh` 前缀校验；`nfc.c` 配对 URL 改 Kconfig | `upstream-touch-allowlist.yaml` 逐条限行数 |
| **T2 禁止** | 改上游测试、锁文件/依赖清单、生成文件、机器人写入文件、CI 工作流、`AGENTS.md` 正文、纯格式化、把业务实现内联进上游文件 | shim 分支的 164 个测试改动、`tts_provider.py` +345 | 同上，命中即失败 |

## 3. T0 技术目录（按平台）

### 后端（Python）——目标：`backend/**` 上游文件改动 = 0

```
backend/fork/
├── main.py            # uvicorn 入口：from main import app; apply_patches(); mount_fork_routers(app)
├── patches/           # 补丁注册表：每个补丁声明 target 符号、替换物、启用条件（profile/env），启动时断言目标存在
│   ├── identity.py    # firebase_admin.auth.verify_id_token → auth_shim.verify_id_token；firebase_admin.initialize_app → no-op（better_auth 时）
│   ├── storage.py     # utils.other.storage 的客户端工厂 → MinIO
│   ├── queue.py       # utils.cloud_tasks 的派发函数 → cloud_tasks_redis
│   ├── providers.py   # STT/TTS/翻译 provider 注册：向上游 provider 表追加 MiMo/MOSS/SenseVoice/…
│   └── brand.py       # prompt 常量中的品牌词替换（直到上游接受 {product_name} 参数化 PR）
├── sitecustomize.py   # 非入口进程（queue worker、jobs、modal 脚本）通过 PYTHONPATH 注入，等价于 main.py 的 apply_patches()
├── routers/           # fork 新增路由（只允许上游没有的能力，且不改变上游路由语义）
├── identity.py auth_shim.py push_provider.py egress_policy.py storage_minio.py cloud_tasks_redis.py
├── stt/ tts/ translation/    # provider 实现（从上游文件里迁出）
└── tests/             # 由 backend runner 发现（backend-test-discovery 清单要求）
```

- 先例：`backend/firestore_pg/compat` 已用 `sys.modules` 别名做到 88 个业务模块零改动——同一思路推广到身份、存储、队列、provider。
- 补丁只替换**模块级符号**（函数/工厂/注册表），不 monkeypatch 类内部；每个补丁在启动自检时 `assert hasattr(target_module, name)`，上游重命名即在 CI 第一时间失败，修补丁而不是改上游。
- 环境变量优先：上游已支持的开关（`FIRESTORE_PG_DSN`、`FIRESTORE_EMULATOR_HOST`、`OMI_ENV_STAGE`、`CORS_ALLOWED_ORIGINS`、`BASE_API_URL`…）直接用，不加补丁。
- 镜像：`deploy/self-host/Dockerfile` `FROM` 上游镜像层，`pip install -r backend/requirements-fork.txt`，`CMD uvicorn fork.main:app`。

### Flutter——目标：`app/lib/**` 上游文件改动 ≤ 白名单（1 行）

- **包级 shim**：`app/pubspec_overrides.yaml`（Dart 官方机制，新文件）把 `firebase_auth`、`firebase_messaging`、`firebase_crashlytics`、`firebase_core`、`firebase_analytics` 指向 `fork/packages/<name>_shim/`，shim 暴露与官方包**同名的公开 API**（`FirebaseAuth.instance`、`User`、`FirebaseMessaging.onMessage`…），内部用 Better Auth / webhook 推送 / no-op 实现。上游 139 处调用零改动；`omi_cloud` profile 不启用 overrides，构建结果与上游一致。
- URL 与开关：上游已有的 `--dart-define`（`OMI_API_BASE_URL`、`OMI_APP_PROFILE`）+ fork 新增 dart-define；profile 表是新文件 `app/lib/env/fork/deployment_profiles.g.dart`（`.gitignore` 之外，由 render 生成并提交）。
- 品牌词：运行时 `LocalizationsDelegate` 包装（一行 T1 注入到 `localizationsDelegates`），同时向上游提 ARB `{appName}` 参数化 PR；不改 ARB。
- 生成文件（`*.g.dart`、`app_localizations*.dart`）差异永不提交。

### Windows（Electron/Vite/TS）——目标：上游文件改动 = 0

- `desktop/windows/vite.fork.config.ts`：`import base from './vite.config'`，追加 `resolve.alias`（`firebase/auth` → `@fork/firebase-auth-shim` 等）与 `define`；构建命令 `vite build --config vite.fork.config.ts`（fork 工作流与 `Makefile.fork`）。
- `VITE_*` 环境变量已是上游机制；profile 表为新文件 `src/shared/fork/deploymentProfiles.generated.ts`，由 shim 包读取。
- `electron-builder.fork.config.mjs` 同法 extend。

### macOS（Swift）——无运行时补丁，T1 集中在 2~3 个文件

- 生成文件放 `Desktop/Sources/Generated/`（SwiftPM 自动纳入、上游已排除格式化）：`Brand.generated.swift`、`DeploymentProfiles.generated.swift`。
- 构建期用 `PlistBuddy` 写入 Info.plist 键（`run.sh` 与 Codemagic 已有此通道）：`OMIDeploymentProfile`、`OMIProductionFamilyBundleIdentifiers`、`SUFeedURL`、`SUPublicEDKey`、TCC 文案。
- T1 白名单：`AppBuild.swift`（生产族 id 改读 Info.plist，≤3 行）、`DesktopBackendEnvironment.swift`（URL 常量改读生成表，≤3 行）、`scripts/app-config.sh`（前缀校验，≤3 行）。三者同时向上游提 PR（上游已用 Info.plist 标记控制 external preview，接受概率高）。
- Better Auth 登录：新增 `Desktop/Sources/Fork/BetterAuthSession.swift`，接入点是 `AuthService.swift` 中 provider 选择的一处 `switch`（T1，≤3 行）。

### Web（Next.js，与上游同步）

- 保持上游 `next.config.js`，Cloudflare 的 vinext 别名是加法且条件化（`VINEXT_BUILD=1`）——T1 白名单项，同时向上游提"可插拔认证提供方"PR。
- Better Auth 代理与 profile 对象是新文件（`src/app/api/better-auth/[...path]/route.ts`、`src/lib/fork/*`）；`firebase.ts` 的开关读 profile 是 ≤3 行 T1。
- 自托管用上游 `web/app/Dockerfile`（Node standalone）原样运行，**不引入 Bun**。

### 固件

- `EXTRA_CONF_FILE=brand.conf`（广播名、DIS）与 `DTC_OVERLAY_FILE` 是 Zephyr 原生机制，零改动。
- `nfc.c:94` 的配对 URL 是 C 字面量：T1 一行改为 `CONFIG_FORK_PAIR_URL`（Kconfig 新增在 fork 的 `Kconfig.fork`，由 `brand.conf` 赋值），同时提上游 PR。

### CI、配置、文档

- 全部走独立文件：`fork-*.yml`、`checks-manifest.fork.yaml`、`deployment-setting-classification.fork.json`、`Makefile.fork`、`AGENTS.fork.md`（**上游 `AGENTS.md` 零改动、不加指针**——预算无余量，见 §4 第 9 条）。

## 4. T1 白名单（初始版，目标随上游 PR 接受逐条删除）

| # | 文件 | 行数上限 | 钩子内容 | 上游 PR 主题 |
|---|---|---|---|---|
| 1 | `desktop/macos/Desktop/Sources/AppBuild.swift` | 3 | 生产族 bundle id 改读 Info.plist 键 | "make production-family identifiers Info.plist-driven" |
| 2 | `desktop/macos/Desktop/Sources/DesktopBackendEnvironment.swift` | 3 | 四个 URL 常量改读生成表 | "backend endpoints from bundle configuration" |
| 3 | `desktop/macos/scripts/app-config.sh` | 3 | bundle 前缀校验改读配置 | "configurable bundle-id prefix for named bundles" |
| 4 | `desktop/macos/Desktop/Sources/AuthService.swift` | 3 | 身份提供方 `switch` 增加 Better Auth 分支 | "identity provider seam" |
| 5 | `app/lib/main.dart` | 1 | `localizationsDelegates` 包装品牌委托 | "ARB {appName} parameterization"（接受后删除此项） |
| 6 | `web/app/next.config.js` | 5 | vinext 条件别名 + 认证提供方开关 | "pluggable auth provider / Workers build" |
| 7 | `web/app/src/lib/firebase.ts` | 3 | `isFirebaseAuthConfigured` 改读 profile | 同 6 |
| 8 | `omi/firmware/omi/src/lib/core/nfc.c` | 1 | 配对 URL 改 Kconfig | "Kconfig-driven NFC pairing URL" |
| ~~9~~ | ~~`AGENTS.md` 系列~~ | — | **已作废**（2026-09-03 实测）：上游把这些文件维护在预算天花板上（`app/AGENTS.md` 11288/11500、`backend/AGENTS.md` 38997/39000），加一行指针即触发 `agents-md-lean` 失败。fork 规则放独立的 `*.fork.md`，上游文件零改动、不加指针 | — |

后端 `backend/**`：**0 条**。上游 CI/测试/锁文件：**0 条**。上游 `AGENTS.md`：**0 条**（见上）。

## 5. 两条测试通道（上游测试永不修改）

| 通道 | 运行什么 | 环境 | 证明什么 |
|---|---|---|---|
| 上游模式 | `backend/test.sh`、`app/test.sh`、Swift/Windows 测试、`web-checks` 原样 | 不设任何 shim/profile 变量；不启用 `pubspec_overrides`/`vite.fork.config` | fork 没有改变上游行为（等价性） |
| fork 模式 | `backend/fork/tests/`、`app/test/fork/`、`Desktop/Tests/Fork*`、`windows/src/**/*.fork.test.ts`、`contracts/` | `OMI_DEPLOYMENT_PROFILE=self_hosted` 或 `cloudflare`；启用别名/补丁 | shim 与 profile 行为正确 |

shim 分支上那 164 个测试改动的等价断言，全部落到 fork 模式的测试目录；`backend-test-discovery` 清单检查保证它们被 runner 发现。

## 6. 守卫与度量

- `scripts/fork/check-upstream-touch.py --base upstream/main --allowlist dev/unified-main/upstream-touch-allowlist.yaml`：对 PR diff 中存在于 `upstream/main` 树的每个文件——不在白名单 → 失败；在白名单但超行数 → 失败；命中 T2 类别 → 失败，并输出对应的 T0 做法提示。进 `checks-manifest.fork.yaml`（`fork-upstream-touch`）。
- 每次上游同步 PR 自动评论"被 fork 修改的上游文件总数"（`comm -12 <(git log --no-merges --name-only --format= upstream/main..main | sort -u) <(git ls-tree -r --name-only upstream/main | sort)`），目标 = 白名单条目数（≤ 12），趋势只降不升。
- 上游 PR 队列记录在 `dev/unified-main/upstream-prs.md`：每接受一个，删一条白名单。


## 7. 首次实战校正（2026-09-03，S0 同步）

第一次按本策略执行同步时暴露的四条，已回写进上面的规则：

1. **上游把受预算约束的文件维护在天花板上。** `app/AGENTS.md` 11288/11500 字节、`backend/AGENTS.md` 38997/39000、`backend/utils/other` 12/12 个源文件。fork 只要加一行或一个文件就会把上游的守卫压垮，而且是**延迟引爆**：加的时候还有余量，上游长满后才炸。推论：fork 文件绝不能放进受阈值约束的上游包，指针也不能加进 AGENTS.md。本次据此把 `storage_minio.py` 移入新建的 `backend/fork/`。
2. **`git rerere` 会静默套用旧解法。** 本仓库 `rerere.enabled=true` 且有 152 条缓存，本次 13 个冲突里有 6 个被自动"解决"且**不留冲突标记**——`grep '<<<<<<<'` 查不出来。同步流程必须以 `git status` 的 `UU` 为准，并逐个复核 rerere 的结果是否符合当期策略。
3. **同步 PR 会稳定触发 5 项与"改动量"挂钩的检查**，与代码质量无关，属于流程样板（见 `06-upstream-sync.md` §7）。
4. **`desktop-e2e-flow-coverage` 无豁免机制**，上游新增 Swift 文件若自带覆盖缺口，同步 PR 就会红；不得为了变绿而编造 e2e 流程。
