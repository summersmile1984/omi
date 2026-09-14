# CI/CD 三阶段主线(Local dev → CI → CD×2)

日期: 2026-09-14 · 分支: `feature/cloud-neutral-shim` @ `2aba718423` · 作者: 本地 dev 阶段落地 + 三阶段梳理

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

- **阶段 1(本地)已达成**:一条命令起全栈,一条命令自证,6/6 通过(2026-09-14,本机)。
- **阶段 2(CI)已存在但不在本分支**:上游清单 + fork 清单双门禁,规则是"差异选择、无密钥即绿"。
- **阶段 3(CD)是两条车道 × 两个 stage**:Cloudflare 与 Server OS 各自 beta/production,手工触发、只认冻结产物。

---

## 1. 阶段 1 —— Local dev(编译 + 运行)

### 1.1 入口

```bash
dev/local.sh up          # 起全栈并等待健康(冷启动约 30s,其中 emulator 镜像已缓存)
dev/local.sh verify      # 端到端自证,写 JSON 证据
dev/local.sh status      # 谁在跑、在哪个端口、健康与否
dev/local.sh restart     # 只重启应用进程(容器与数据保留)
dev/local.sh logs backend
dev/local.sh down        # 停进程 + 停容器(保留数据卷)
dev/local.sh reset       # 停 + 删数据卷(本地数据可丢,重建即可)
```

`make -f Makefile.fork local-up|local-verify|...` 是同一件事的 make 包装。
fork 目标放在 `Makefile.fork` 而不是上游 `Makefile`:上游文件每改一行都是下一次同步的冲突源。

### 1.2 起了什么

| 组件 | 形态 | 端口 | 说明 |
|---|---|---|---|
| postgres | 容器 | 5442 | `firestore_pg` shim 的落点,与生产同形 |
| redis | 容器 | 6379 | 队列 + 缓存(`QUEUE_BACKEND=redis`) |
| minio | 容器 | 9100/9101 | 对象存储,替代 GCS(`STORAGE_BACKEND=minio`) |
| firebase emulators | 容器 | 8080/9099/9199 | 本机登录流程用(Firestore/Auth/Storage) |
| auth-server | 进程 | 3000 | Better Auth:签发 JWT + JWKS |
| queue-worker | 进程 | — | 4 条队列(sync/audio-merge/account-deletion/finalization) |
| backend | 进程 | 8100 | FastAPI,全 shim env |

状态与日志在 `.local/local-dev/`(pid、logs、evidence),已被 gitignore。

### 1.3 "编译"这一步在这个仓库是什么

| 目标 | 命令 | 现状 |
|---|---|---|
| backend 依赖 | `make setup-backend`(`uv pip sync pylock.macos.toml`) | 已有,锁定版本 |
| backend 语法/编译 | `python -m compileall main.py firestore_pg utils routers` | **已纳入 `local.sh up`**,编译不过就不启动 |
| backend 运行 | `uvicorn main:app --port 8100` | 已纳入 |
| auth-server 依赖 + 迁移 | `npm run migrate` | 已纳入(`up` 里自动跑) |
| firestore-pg 迁移 | `python scripts/firestore_pg_migrate.py migrate` | **已纳入**(与生产 `firestore-pg-migrate` 同一个入口) |
| 桌面端(macOS) | `desktop/macos/run.sh` | 独立目标,不在 `local.sh` 内(见 §1.6) |
| 移动端(Flutter) | `app/test.sh` / `flutter build` | 独立目标 |
| Web | `web/app/test.sh`(`bun run check`) | 独立目标 |

### 1.4 自证:`dev/local.sh verify`

六个检查,全部走生产代码路径或真实网络往返,任一失败即整体失败:

| # | 检查 | 证明的事 |
|---|---|---|
| 1 | postgres | `firestore_pg` 已迁移:2 个 migration、136 个 collection、109 张表 |
| 2 | redis | PING + set/get/delete 往返 |
| 3 | storage | 经 storage shim 上传 → `get_user_has_speech_profile` 可见 → 下载字节完全一致(8044B) |
| 4 | queue | 4 条队列的 worker↔handler 秘钥契约:worker 会出示的 secret 被接受,错的被 403;worker 进程存活 |
| 5 | auth | Better Auth 签发 JWT 被后端接受;同一请求不带 token 被拒(边界双向验证) |
| 6 | backend | `GET /v1/health` 200 |

证据文件:`.local/local-dev/evidence/local-verify-<UTC>.json`。

**2026-09-14 本机结果:6/6 PASSED**(冷启动后复跑同样 6/6)。

### 1.5 设计约束(为什么这样写)

1. **不依赖任何云凭证**:本地不需要 OpenAI/Deepgram/Gemini/Anthropic 的 key,也不需要 GCP/Firebase 账号。
   (上游 `make dev-up` 走的是另一套:Firebase 模拟器 + 云 provider key,`PROVIDER_MODE=offline` 才能脱云。)
2. **端口可配**:`dev/local.env`(gitignore)覆盖 `dev/local.env.example`;优先级 `环境变量 > local.env > example`。
   本机 5434/9000/9001 被无关容器占用,所以本地栈用 5442/9100/9101 —— 本地 dev 绝不要求你去停别的项目。
   `dev/local.sh ports` 会打印占用者并在冲突时拒绝启动。
3. **与生产同形**:本地少的只是副本数、TLS、备份;组件与 env 契约(哪些桶、哪些队列、哪个 shim)一致。
4. **失败要说人话**:老本地库会被 `firestore_pg` 迁移拒绝,`up` 会直接给出 `dev/local.sh reset` 的指引;
   queue worker 环境不全会秒退,`up` 会在 3 秒后检查进程存活并报错。
5. **自测不许有副作用**:`dev/tests/test_local_sh.py` 只跑只读命令(help/ports/env)。
   `--stop` / `--no-backend` 会操作真实容器与进程,所以在测试里绝不调用 —— 这条是踩过坑之后写下的:
   早期版本的测试用 `--stop` 验证兼容性,结果把正在运行的本地栈真的拆了。

### 1.6 本阶段已知边界

- 桌面端/移动端/Web **不**由 `local.sh` 拉起。桌面端要连本地后端时,先 `eval "$(dev/local.sh env)"` 再运行
  `desktop/macos/run.sh`(named bundle 启动参数见 `dev/deploy-local-ledger.md`)。
- 本地 AI 能力是 operator 配置项:不配 `MIMO_*`/`MOSS_API_KEY`/`TRANSLATION_PROVIDER` 时后端照常启动,
  只是 AI 功能不可用 —— 因此 verify 不检查 AI,只检查数据面/认证/队列。
- 只支持单机单实例(容器名固定),同一台机器上同时开两个本地栈不受支持。

---

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
| 本地 dev 一键起栈 + 自证 | ✅ `dev/local.sh`(本次落地,6/6) | 无对应入口 |
| 上游清单(local/ci 双 lane) | ✅ 163 条 | ✅ |
| fork 清单 + `scripts/fork/*` | ❌ 不在本分支 | ✅ `checks-manifest.fork.yaml`、`fork-checks.yml` |
| CD 两条车道 | ❌ 不在本分支 | ✅ `fork-cd-cloudflare.yml` / `fork-cd-server.yml` |
| self-host compose + 验收脚本 | ✅ | ✅(超集) |

未完成项(按上表顺延):

1. **阶段 1**:把桌面端/移动端接进 `local.sh`(至少提供 `local.sh env` + run.sh 的一步式脚本);
   目前是"手动两步"。
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
