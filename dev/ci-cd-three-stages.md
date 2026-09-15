# CI/CD 三阶段主线(Local dev → CI → CD×2)

日期: 2026-09-14 · 分支: `main` @ `4c0885b306`(上游 v0.12.348 已并入)· 作者: 本地 dev 阶段落地 + 三阶段梳理

本文是 fork 交付主线的**单一口径**:三个阶段、一条流水线、产物只向前流动、下游不重新构建。
阶段 1 已经在当前 checkout 上跑通并留下证据;阶段 2/3 的接线主要落在 `origin/main`,
本文标注了每个文件所在的 revision,避免把别处的实现当成这里的现状。

```
   ┌──────────────────────────────┐    ┌───────────────────────────┐    ┌──────────────────────────────┐
   │ 阶段 1  Local dev            │    │ 阶段 2  CI                │    │ 阶段 3  CD ×2                │
   │ 编译 + 运行 + 自证            │ →  │ 同一份清单,本地=CI         │ →  │ 一次冻结,两条独立车道          │
   │                              │    │                           │    │                              │
   │ dev/local.sh up              │    │ make preflight (local)    │    │ fork-release-prepare.yml     │
   │ dev/local.sh verify          │    │ repo-checks.yml (ci)      │    │   ├─ fork-cd-cloudflare.yml  │
   │                              │    │ fork-checks.yml           │    │   └─ fork-cd-server.yml      │
   │ 无云凭证 · 端口可配 · 与生产同形 │    │ 无密钥即绿 · 差异选择       │    │ beta / production 各一条       │
   └──────────────────────────────┘    └───────────────────────────┘    └──────────────────────────────┘
         证据: verify-*.json                 证据: CI attestation            证据: delivery + qualification
```

---

## 0. 一句话结论

- **阶段 1(本地)**:数据面 harness 已在 main 上跑通(4/4,1 项按需跳过);Server OS 运行时是镜像形态,
  本地需要 amd64 构建通道,见 §1.2。
- **阶段 2(CI)**:上游清单 + fork 清单双门禁,规则是"差异选择、无密钥即绿";fork 清单与两条 CD 车道都在 main 上。
- **阶段 3(CD)**:两条车道 × 两个 stage——Cloudflare 与 Server OS 各自 beta/production,手工触发、只认冻结产物。
  **2026-09-14 复核:Cloudflare 产品契约 `bash deploy/cloudflare/ci/product.sh` 在合并后的 main 上通过。**

---

## 1. 阶段 1 —— Local dev(编译 + 运行)

> **2026-09-14 在 main 上复核后的口径**:上游 v0.12.348 合并进 main 之后,Server OS 的运行时
> 是**镜像形态**——profile 表、语音/LLM 模型库、Qdrant/Typesense/SearXNG 都在镜像里
> (`deploy/self-host/Dockerfile` 用 `scripts/profiles/render.py --target self_hosted --stage <stage>`
> 渲染)。因此本地 dev 分成两条,别再混为一谈:

### 1.1 数据面 harness(本机、无镜像,秒级)

```bash
dev/local.sh up        # postgres + redis(带密码) + minio + firebase emulators + 两个迁移 + auth-server
dev/local.sh verify    # 4 项自证 + 1 项(backend)按需跳过,写 JSON 证据
dev/local.sh selfhost  # 打印 Server OS 运行时需要什么、为什么 checkout 跑不起来
dev/local.sh status | restart | logs | ports | env | down | reset
```

`make -f Makefile.fork local-up|local-verify|local-status|local-down|local-reset|local-selftest` 等价。

自证内容(全部是真实往返):

| # | 检查 | 证明的事 |
|---|---|---|
| 1 | postgres | `firestore_pg` 已迁移(9 个 migration / 159 collections / 132 张表) |
| 2 | redis | 带密码认证的 PING + set/get/delete(与 `REDIS_DB_PASSWORD` 契约一致) |
| 3 | storage | 直连 MinIO 的 put/head/get/delete 往返 |
| 4 | auth | Better Auth **真实注册** → `/auth-issue` 签发会话 JWT → JWKS 可取 |
| 5 | backend | 仅当你自己起了 backend 才检查,否则明确 SKIP(不是假装通过) |

**2026-09-14 在 main 上:4/4 PASSED,1 SKIPPED**(证据 `.local/local-dev/evidence/`)。

### 1.2 Server OS 运行时(本地已可跑通)

镜像路径(`operations.sh start`)仍需要 amd64 构建通道;但在 checkout 上也能跑起来,
用 core-only profile(**与 `deploy/self-host/ci/product.py::core_only_profile` 同一形状**:
去掉 speech/LLM 模型库,保留 embedding):

```bash
dev/local.sh up            # 数据面:postgres/redis/minio/qdrant/typesense/emulators + auth-server
dev/selfhost-local.sh up   # 渲染 core-only profile → qdrant 迁移 → uvicorn fork.main:app
dev/local.sh verify        # 5/5(backend 从 SKIP 变 PASS)
dev/selfhost-local.sh stop # 停后端并把生成的 profile 表还原
```

env 模板:`dev/selfhost-local.env.example` → 复制为 `dev/selfhost-local.env`(gitignore)。
`up` 结束后可直接走真实业务链路:

```
无 token  GET /v1/conversations → 401
带 token  GET /v1/conversations → 200 []
```

(token 由 auth-server 真实注册 + `/auth-issue` 签发;后端经 `/internal/verify` 校验会话,
因此 auth-server 与后端共享 `AUTH_INTERNAL_ADMIN_SECRET`。)

**2026-09-14 实测**:`dev/local.sh verify` 5/5,业务接口 401→200,
`firestore_pg/tests/test_transaction_semantics.py` + `test_deletion_write_fence.py` 39 passed、
`fork/tests/test_account_deletion.py` 10 passed。

仍然需要 amd64 通道的场景:`deploy/self-host/build-images.sh`(镜像交付)与完整
profile(speech/LLM 模型库:`prepare-speech.py` 目前因上游 TTS 归档 digest 与固定值不符而
无法完成,见下)。

### 1.3 设计约束

1. **不依赖云凭证**:数据面 harness 不需要任何云 key。
2. **端口可配**:`dev/local.env`(gitignore)覆盖 `dev/local.env.example`;优先级 `环境变量 > local.env > example`;
   `dev/local.sh ports` 打印占用者并在冲突时拒绝启动。
3. **失败要说人话**:老本地库给出 `dev/local.sh reset`;harness 与运行时的边界写在 `dev/local.sh selfhost`。
4. **自测无副作用**:`dev/tests/test_local_sh.py` 只跑只读命令(help/ports/env)。

## 2. 阶段 2 —— CI

### 2.1 两个门禁,同一套规则

| 门禁 | 清单 | 本地命令 | CI 命令 |
|---|---|---|---|
| 上游 | `.github/checks-manifest.yaml`(163 条检查) | `make preflight` | `scripts/pr-preflight`(lane 默认 `ci`) |
| fork | `.github/checks-manifest.fork.yaml`(origin/main) | `scripts/fork/preflight [--fork-only]` | `fork-checks.yml` → `run_checks.py --manifest ... --lane ci` |

关键不变量:**清单里的每条检查都必须同时声明 `local` 与 `ci` 两个 lane**(`run_checks.py` 会拒绝只声明一个的条目),
所以"本地绿、CI 红"在结构上不可能由清单漂移造成。

### 2.2 事件 → lane(origin/main 的定型)

| 事件 | runner | 差异基准 | 能否授权发布 |
|---|---|---|---|
| PR → main | 托管 `ubuntu-latest`(+ `macos-26`) | 目标分支 | **不能** |
| push 到 main/codex/** | 自托管 `mac-studio/memweft` | `event.before` | **不能** |
| `workflow_dispatch`(release) | 自托管 | 完整清单 + attestation | **能** |

规则:**CI 全部 hermetic,不需要业务密钥**。唯一在 PR lane 里碰凭证的是 `runtime_image_contracts.yml`,
它自带"没有 GCP_CREDENTIALS 就跳过 image-smoke"的降级分支。

### 2.3 阶段 1 → 阶段 2 的衔接

- 阶段的产物不是"能跑就行",而是"同一份命令可在 CI 复现":`dev/local.sh` 里的编译/迁移步骤,
  在 CI 对应 `backend/scripts/run-unit-ci.sh`、`bash backend/test.sh`、`app/test.sh`、`web/app/test.sh`。
- 新增检查一律进清单,不写进 workflow YAML;本地先用 `scripts/fork/preflight --fork-only` 跑一遍。

### 2.4 上游同步后先跑 owner 审计,别让门禁一次只报一个

三个客户端 stage 在构建时各自校验"上游 source owner"摘要,而且**遇到第一个不匹配就停**
(`desktop/macos/fork/swift_overlay.py`、`app/fork/prepare.py`、`desktop/windows/fork/prepare.py` 都是这个形状)。
一次上游同步可能同时让多个 owner 失效 —— v0.12.348 一次弄脏 8 个;Electron 这次 5 个。
若靠门禁逐个报,每个 owner 要付一轮 CI。

所以 `fork-overlay-owner-audit` 在评审开始前一次性列出**全部**陈旧 owner:

```bash
python3 scripts/fork/check-overlay-owners.py   # flutter 85 / desktop 26 / electron 161
```

三条不变量(由 `scripts/fork/test_overlay_owner_audit.py` 断言):

1. **每个 stage 校验的注册表都被审计**(flutter、macOS/Swift、Electron 三份 `source-owners.json`);
2. **任何能让被覆盖 owner 失效的 diff 都会选中这条审计** —— 含 `desktop/windows/**`,以及审计自身的源码;
3. 重录摘要前必须先看"上游改了什么":语义变化要改 fork 替换代码,不是改摘要。

---

## 3. 阶段 3 —— CD(两条车道 × 两个 stage)

### 3.1 形状

```
fork-release-prepare.yml   (workflow_dispatch, 必须给一个成功的「完整 Fork Checks」run id)
  ├─ 冻结 delivery-<sha>-<brand>-<stage>-cloudflare    → cloudflare.tar.gz + delivery.json
  ├─ 冻结 delivery-<sha>-<brand>-<stage>-self_hosted   → server-images.tar + delivery.json
  ├─ Cloudflare artifact qualification
  ├─ Server image qualification
  └─ Release ready (runtime and public ingress)        ← 只有这 4 个 job 全绿才算可发布

  ├─ fork-cd-cloudflare.yml  → environment: cloudflare-{beta,production}  (180 min, concurrency 独占)
  └─ fork-cd-server.yml      → environment: server-{beta,production}      (120 min, concurrency 独占)
```

- **两个 stage**:`beta` 与 `production`,同一套 workflow、同一份产物,差别只在 environment(审批人、变量、目标主机)。
- **两条车道**:Cloudflare(Workers/D1/R2/Queues)与 Server OS(self-host compose 镜像)。二者互不依赖,
  同一份 delivery 各自独立发布。
- **只认冻结产物**:CD 阶段不重新构建,admission 校验的是 delivery 的 sha + stage + target,
  历史上"只过了构建""跳过 qualification"的 run 一律拒绝。
- **手工触发**:`workflow_dispatch`,且必须 `--ref main`;源码没进 main 就不能发。

### 3.2 与阶段 1 的同形关系

生产就是本地栈的超集:`deploy/self-host/compose.production.yml` 比 `dev/docker-compose.dev.yml` 多了
qdrant/typesense/searxng/llm 等可选服务与 TLS,但同一批镜像、同一套 env 契约、同一个迁移入口
(`scripts/firestore_pg_migrate.py`)。阶段 1 验证过的 shim 行为,在阶段 3 里是同一份代码。

### 3.3 运维侧门禁(operator 手动)

```bash
make self-host-config-check           # 静态契约 + compose config
make self-host-migration-gate         # 迁移/cutover 前置门
make self-host-ops-check              # operations.sh self-check
make self-host-zero-vendor-acceptance # 零厂商依赖验收
```

注意:这四个目标**没有被任何 workflow 引用**,是 operator 手动跑的;CD 车道走的是 `scripts/fork/deploy_server.py`。

---

## 4. 让三个阶段成为"一条线"的不变量

1. **同形**:本地栈与生产栈是同一套组件与 env 契约,差别只在规模/配置。
2. **只编译一次**:阶段 1 编译、阶段 2 测试、阶段 3 运输冻结产物,CD 内不重建。
3. **同一份命令**:清单同时声明 local/ci 两个 lane,`make preflight` 与 CI 是同一批检查。
4. **凭证延后**:阶段 1、2 零业务凭证;凭证第一次出现是在阶段 3 的 environment 里。
5. **每阶段留证**:本地 `verify-*.json` → CI attestation → release qualification/delivery。

---

## 5. 现状与差距(按 revision 如实标注)

| 能力 | 本分支 `feature/cloud-neutral-shim` | `origin/main` |
|---|---|---|
| 本地 dev 数据面 + 自证 | ✅ `dev/local.sh`(已并入 main,4/4 + 1 skip) | ✅ 同左 |
| 上游清单(local/ci 双 lane) | ✅ 163 条 | ✅ |
| fork 清单 + `scripts/fork/*` | ❌ 不在本分支 | ✅ `checks-manifest.fork.yaml`、`fork-checks.yml` |
| CD 两条车道 | ❌ 不在本分支 | ✅ `fork-cd-cloudflare.yml` / `fork-cd-server.yml` |
| self-host compose + 验收脚本 | ✅ | ✅(超集) |

未完成项(按上表顺延):

1. **阶段 1**:Server OS 运行时的本地入口(`operations.sh start`)需要一条能在 arm64 上完成的
   镜像通道(amd64 builder 或 CI);在此之前它无法在本机验证。
2. **阶段 2**:本分支合入 main 后,把 `dev/local.sh` 的编译步骤登记为 fork 清单里的一条检查
   (`local` 与 `ci` 两个 lane),否则它只是"能跑的脚本",不是"受保护的门禁"。
3. **阶段 3**:Server OS 镜像目前只在本地打 tag,没有任何 registry 推送;Cloudflare 侧远端资源尚未完整创建。
   beta 车道要真正可用,需要先补齐 registry/交付通道(见 `scripts/fork/RELEASE.md`)。
4. **文档债**:`AGENTS.md` 里的 `RELEASEWITHBACKEND`、`backend-test-discovery` 与 `desktop_auto_release.yml`
   的 break-glass 描述与仓库现状不符(前者工作流已 `disabled_manually`、无 `branch` 入参;
   后者全仓库不存在该 manifest 检查;desktop 真正的应急入口是 `desktop_breakglass_rollout_beta.yml`)。
   建议在三阶段口径确定后一并修正。
5. **阶段 2 的"首个失败即停"**:`run_checks.py` 默认 `keep_going=False`,fork 门禁因此**每次只报一条**失败检查。
   2026-09-14 修 Electron owner 时,这一条链被逐个揭开:electron → repo-state → flutter anchor,
   每条都要付一轮完整 CI(约 4 分钟 + 排队),而三者其实互不相关、可以一次全报。
   `.github/scripts/run_checks.py` 内部已有 `keep_going`(第 420 行),但**没有 CLI 开关**;
   把它暴露出来属于上游文件改动(T0/T2 边界),在 fork 侧包一层"逐条跑完再汇总"则会动到
   发布授权车道的 attestation 语义 —— 两者都需要一次明确的决定,故此处只记录,不擅自改。

---

## 6. 现在怎么用(最小闭环)

```bash
# 阶段 1:本地跑起来并自证
dev/local.sh up
dev/local.sh verify          # 期望 6/6 PASSED
dev/local.sh status

# 阶段 1 → 阶段 2:提交前跑同一批检查
make preflight               # 上游清单 local lane(163 条,按差异选择)
# 合入 main 后:scripts/fork/preflight --fork-only

# 阶段 3:手工发布(需要 main + 完整 Fork Checks 的 run id)
gh workflow run fork-release-prepare.yml --ref main -f ci_run_id=<RUN_ID> -f stage=beta
gh workflow run fork-cd-cloudflare.yml --ref main -f delivery_run_id=<RELEASE_RUN_ID> -f stage=beta
gh workflow run fork-cd-server.yml     --ref main -f delivery_run_id=<RELEASE_RUN_ID> -f stage=beta
```

---

## 附:本次改动

| 文件 | 作用 |
|---|---|
| `dev/local.sh` | 本地栈生命周期 CLI(up/status/verify/restart/logs/ports/env/down/reset) |
| `dev/local.env.example` / `dev/local.env` | 端口与 dev 密钥的分层配置(后者 gitignore) |
| `dev/local_verify.py` | 6 项端到端自证 + JSON 证据 |
| `dev/tests/test_local_sh.py` | 8 条 hermetic 自测(配置分层、端口冲突、命令面) |
| `dev/deploy-local.sh` | 兼容壳,转发到 `dev/local.sh` |
| `dev/docker-compose.dev.yml` | Redis/MinIO 端口参数化 + Redis healthcheck |
| `Makefile.fork` | fork 自有 make 目标(不动上游 Makefile) |
| `.gitignore` | 忽略 `dev/local.env` |

---

## 1.4 本地环境 ↔ 生产环境 对照(2026-09-14 实测)

| # | 生产目标 | 本地对应环境 | 命令 | 实测结果 |
|---|---|---|---|---|
| 1 | **Server OS 自托管** | 数据面 + core-only 自托管后端 | `dev/local.sh up` ; `dev/selfhost-local.sh up` | ✅ `verify` 5/5;业务接口 401 → 200 `[]` |
| 2 | **Cloudflare** | 本地 target(workers + 本地绑定 + Provider 替身) | `npm run dev:product -- --output <dir>` | ✅ `/health` `/v1/health` `/` = 200;业务路由 401 |
| 3 | **客户端** | 各自 fork stage 在构建时解析 `<target>.local` 并注入端点 | 见下 | web ✅;desktop 已接线未构建;flutter 阻塞在 SDK 版本 |

### 客户端本地运行入口(统一)

```bash
dev/local-client.sh web      [--target self_hosted|cloudflare] [--stage local] [--port 3210]
dev/local-client.sh desktop  [--target ...] [--stage ...]
dev/local-client.sh mobile   [--target ...]
dev/local-client.sh stop web
```

**本机装 pinned Flutter(侧装,不动已有的 SDK)**:

```bash
curl -fL -o /tmp/flutter-3.44.5.zip \
  https://storage.googleapis.com/flutter_infra_release/releases/stable/macos/flutter_macos_arm64_3.44.5-stable.zip
shasum -a 256 /tmp/flutter-3.44.5.zip   # 442aece6674c4334d46a4f110008a44e835ff53979a8f317333c5e71ccc065b4
ditto -x -k /tmp/flutter-3.44.5.zip "$HOME/flutter-3.44.5"
mv "$HOME/flutter-3.44.5/flutter"/* "$HOME/flutter-3.44.5/"   # zip 多一层 flutter/
cd app && "$HOME/flutter-3.44.5/bin/flutter" pub get --enforce-lockfile
```

`dev/local-client.sh mobile` 依次找 `OMI_FLUTTER_BIN` → `~/flutter-3.44.5/bin` → PATH,
所以侧装后无需改 PATH。`flutter pub get` 会重写 `app/ios/Flutter/ephemeral/**` 里的生成物
(本机缺 `.packages/` 时内容与仓库里的不同),那是生成文件,提交前
`git checkout -- app/ios/Flutter/ephemeral` 还原即可。

每个客户端保留自己的实现,共享的只有 **profile**(`<target>.<stage>`)—— 这正是"环境对得上"的机制。
`self_hosted.local` 把客户端指向 `http://127.0.0.1:8100`,也就是 `dev/local.sh up` +
`dev/selfhost-local.sh up` 起的后端。

| 客户端 | 入口做了什么 | 本机实测 |
|---|---|---|
| web | `bun deploy/web/build.ts --target <t> --stage <s>` → 起 `artifact/start.js` 并等待健康 | ✅ `Built 28 routes for self_hosted.local`;`/conversations` **200** |
| desktop | `desktop/macos/fork/compile.sh`(staged 编译两个 target;**不安装、不启动**,这是 fork 的设计) | ✅ `Build complete! (85.71s)`,两个 target 均产出二进制 |
| mobile | 自动解析 pinned SDK(见下),再跑 `app/fork/test.sh` | ✅ 本机已装 `~/flutter-3.44.5`(Flutter 3.44.5 / Dart 3.12.2):staged `pub get` → `build_runner`(7 outputs)→ `flutter test` **20/20 all passed** → `build bundle`,exit 0 |

### 客户端(第三条)的真实状态

端点注入**已经实现**,分别在各自的 fork stage 里:

| 客户端 | 本地/CI 入口 | profile 注入点 | 本机状态 |
|---|---|---|---|
| Web | `deploy/web/ci.sh`;`bun deploy/web/build.ts --target <t> --stage <s> --output <dir>` | `deploy/web/profile_input.py`(→ `scripts/profiles/render.py`) | ✅ `ci.sh` exit 0(8/8);`--target self_hosted --stage local` → `Built 28 routes for self_hosted.local`,产物含 `127.0.0.1:8100/3000/3001` |
| macOS desktop | `desktop/macos/fork/{compile,build,test}.sh` | `desktop/macos/fork/prepare.py:149` 解析 `<target>.local`,第 296-299 行 `setenv OMI_PYTHON_API_URL / OMI_DESKTOP_API_URL / OMI_AUTH_API_URL / OMI_SHARE_BASE_URL`;`build.py` 写 `ForkDeploymentProfile` 进 Info.plist | 接线已在;本机未构建验证(xcodebuild 有,.build 缓存 1.6G) |
| Flutter | `app/fork/test.sh` | `app/fork/prepare.py:82` 解析 `<target>.local`,并替换上游 `env/environment_profile.dart` 的导入(所以 `environment_profile.dart` 里那份硬编码枚举不影响 staged 构建) | ✅ 本机装了仓库钉的 **3.44.5 / Dart 3.12.2**,与 CI(`subosito/flutter-action`, `flutter-version: 3.44.5`)同一版本 |

注意:仓库里**提交的**四份生成表(`app/lib/env/fork/*.g.dart`、macOS/Windows/Web 的 generated)是
`omi_cloud` 默认渲染;fork stage 会在构建时按 target/stage 重新解析,所以"表里没有 self_hosted 行"
不等于客户端连不上本地后端。

### 三个本地环境的共同点

后端两条各自实现(`backend/` 10k py vs `deploy/cloudflare/` 2.2k py + 176 ts),
**客户端只认 profile**:`self_hosted.local` 的 `api_base_url` 就是 `http://127.0.0.1:8100/`,
与本地后端一致。所以"环境对得上"靠的是 profile,不是共享代码。

### CI 侧对应

| 目标 | CI lane | 运行位置 | 状态 |
|---|---|---|---|
| Server OS | `fork-selfhost-product-core` → `deploy/self-host/ci/product.sh` | PR: `ubuntu-latest`(原生 amd64);push/manual: **自托管 x86**(`linux-x64`) | ✅ 2026-09-14 起绿;PR lane 180s,release lane 在 x86 上 250.59s,16 个 product case 全过 |
| Cloudflare | `fork-cloudflare-product-core` → `deploy/cloudflare/ci/product.sh` | 同上 | ✅ 本机 exit 0 |
| Web | `deploy/web/ci.sh` | — | ✅ 本机 exit 0 |
| 桌面 / 移动 | `desktop-swift-ci` / `mobile-app-checks` | macos-26 / hosted | 需按 profile 构建 |

**Server OS lane 变绿的三步**(2026-09-14,PR #43/#47 实测):

1. `command-09.log` 之前只存在于 runner 的临时目录 —— 先把它 pin 成 artifact(#43),才看见真实错误。
2. 真实错误是 **MinIO 的 Docker Hub 仓库已下线**:`pull access denied for minio/minio`。
   同一 release 在 Quay 上 manifest digest 逐字节相同,所以只换 registry 前缀;
   本地 `dev/docker-compose.dev.yml` 同步改成与生产**同 release 同 digest**(它此前是 `:latest`)。
   这不只是 CI 问题:自托管部署本身也起不来。
3. fixture 随后跑通 15/16,最后一个是契约过严:Server OS 的 JIT envelope 多一个**上游的可选字段**
   `budget_contract_version`(route 没开 `response_model_exclude_none`,所以总是以 null 出现)。
   `contracts/deployment/**` 是 fork 自有,按"允许新增可选响应字段"放宽,并把失败信息改成
   打印 missing/unexpected(原来只说 "wire fields differ",这次为此多花了两次定位)。

**x86 runner 已接上(2026-09-14)**:`turing-agents-MotherBoard-Series`(Linux X64,labels `omi,linux-x64`)
先探测再路由 —— `fork-runner-probe.yml`(`workflow_dispatch` only)报告:Ubuntu x86_64、32 vCPU、90 GiB 内存、
1.5 TB 空闲、**Docker 29.7.2 + Compose v5.4.0**,`pull`/`run` 均 OK(只有 `bun` 缺失,工作流自己装)。
于是可信车道改为:

| 事件 | 之前 | 现在 |
|---|---|---|
| `pull_request` | `ubuntu-latest` | `ubuntu-latest`(**不变**:不可信代码不进自托管机) |
| `push` | Mac Studio(arm64 模拟 amd64) | 自托管 **x86** |
| `workflow_dispatch`(release) | Mac Studio(arm64 模拟 amd64) | 自托管 **x86** |

macOS 原生 job 仍用 Mac Studio;job 名字未变,所以 repo-state 策略与 required checks 不受影响,
release admission 仍按前缀合并同一对 attestation。`runs-on` 用常量 `fromJSON('[...]')`:
上游 Repo Checks 的 actionlint 读 `.github/actionlint.yaml`(只认 `macos`),字面量自定义 label 会直接报
`[runner-label]`;fork 自己的 lint 会解析该常量并按 fork 目录校验。

**新的头号缺口(阶段 2)**:`dead-code-ratchet`(属上游 Hygiene)在 `main` 上就是红的 ——
7 个 `app/lib/fork/identity/*.dart`(只被 staged overlay 引用,上游分析器看不到)加 1 个
`desktop/windows/src/shared/fork/deploymentProfiles.generated.ts`(render.py 产出、Electron 侧
没有任何消费者)。

它的**触发路径含 `backend/**/*.py`**,所以任何 backend Python 改动都会把它选中 —— 包括 fork 自有的
`backend/fork/**`(shim/部署目标代码,恰恰是政策给的正门);选中后它因为上面那 8 条既有发现而必然红,
于是 required 的 Hygiene 卡住合并,而失败内容与本次改动**无关**。补救通道
(写 `.github/scripts/dead_code/*.allowlist.json`、`--update-baseline`)都在 fork 不改的上游路径里,
所以只有两条路:把 Hygiene 降为 advisory,或做结构性迁移让那 8 条真绿
(Flutter identity 也做成 overlay 输入 `*.dart.txt` 由 `prepare.py` 落盘;Windows 那份 profile
要么被 Electron 消费、要么 render 不再产出)。两条都是设计/策略选择,单独一个 PR 做。

**别把它误读成"不能改 backend"**。政策(`dev/unified-main/upstream-touch-allowlist.yaml`)的真实分工是:

| 改什么 | 允许吗 | 代价 |
|---|---|---|
| 新增/修改 **fork 自有**的 `backend/fork/**`(shim、patches、部署目标适配、fork 测试) | ✅ 正门,随便改 | 上游改了被 patch 的符号时,绑定要跟着重做(已立失败类 `FC-fork-patch-binding-follows-upstream-signature`) |
| 直接改**上游已有**的 backend 模块 | ⚠️ 默认禁止(`backend/**` 在 T2 的 `forbidden_patterns` 里),因为它属于"把业务实现内联进上游文件"那一类 | 确实没有运行时补丁缝时走正式豁免:把**精确路径**写进 `forbidden_exceptions`,再加一条带预算的 `allow`(≤3 行 + reason + upstream_pr);每次同步重做 |
| 改上游的**非 backend** 文件(客户端 call site 等) | ⚠️ 需入白名单:≤3 行、带 reason 与 upstream_pr | 每次同步上游都要重新施加一次 |

所以"部署目标相关、shim 相关"的 backend 改动正是设计意图;挡住合并的不是这条政策,而是上面那条被
无关既有红拖垮的上游检查。

政策文档自己的措辞就是"**能不改上游代码就不改**"(`dev/unified-main/00-upstream-touch-policy.md`):默认零改动是**取舍**,
不是铁律 —— 代价是每周合并上游时的冲突面。`forbidden_exceptions` 就是给"这次确实没有别的缝"准备的一次性豁免通道
(精确路径,禁通配符),走它仍然要登记预算与上游 PR,并且同步后重做。当前仓库实测:
`check-upstream-touch.py --aggregate` → `OK: 2 upstream file(s) changed, all within the allowlist`
(`app/lib/flavors.dart +3/3`、`desktop/macos/docs/desktop-updates.mdx +1/1`)。

**第二个缺口(阶段 3 的 release 车道)**:完整清单(release lane)会在 `fork-cloudflare-routes` 停下,
而 diff-scoped 的 PR/push 车道根本不会选中它 —— 所以它一直没露面:

```
FAIL: backend route inventory is stale:
 added   GET /v1/dev/user/daily-summaries, GET /v1/dev/user/daily-summaries/{summary_id},
         GET /v1/static-map, GET /v3/speech-profile/stt-availability,
         POST /v1/conversations/{conversation_id}/mutations, POST /v1/users/developer/button-event
 removed POST /v1/webhooks/sentry, POST /v1/webhooks/sentry/poll
```

即上游 v0.12.348 的路由面跑在了 fork 已提交的 inventory 前面。修法本身是机械的
(`route_inventory.py --write`),但那等于给 6 个上游路由身份背书,并会接着跑该检查的 Cloudflare 一半,
所以它是**独立的一次改动**,不搭在路由 PR 上。
