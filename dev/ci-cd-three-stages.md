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

### 1.2 Server OS 运行时(镜像,本地需要 amd64 构建通道)

```bash
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
  deploy/self-host/operations.sh start
```

前置:从 `.env.production.example` 复制出**非 `.example`** 的 env 文件(检查器明确拒绝示例文件)、
填掉所有 `REPLACE_*`、`PUBLIC_*` 必须与渲染出的 `self_hosted.local` 一致
(`python3 deploy/self-host/check-config.py --env-file <file>` 会逐条校验)。

本机(Apple Silicon)的两个约束:

- `deploy/self-host/build-images.sh` 要求 `BACKEND_PLATFORM=linux/amd64`,镜像需模拟构建
  (本机复现 fixture 构建 20 分钟超时);镜像步骤应交给 amd64 builder 或 CI。
- `self_hosted.local` profile 声明了 speech(SenseVoice + Kokoro)与 Ollama `qwen3:1.7b`,
  这些模型库必须先在位。

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
