# 标准 Server OS：代码审计、部署验收与下一步行动方案

三路入口：[Cloudflare](02-cloudflare-action-plan.md) · [白牌终端](03-whitelabel-action-plan.md) · [统一主线索引](../README.md)。

审计日期：2026-09-04。统一基线为 `origin/main=d238a85af9d999992d9f0352db682cd9f11fc951`，审计工作树分支 `codex/audit-server-20260904`。结论只适用于这个 SHA；不把旧 `feature/cloud-neutral-shim` 分支、既有容器或历史发布报告算作当前主线的部署成功。

**判定：当前主线不能按文档直接交付标准 Server OS 版本。** 已有 PG/Redis/MinIO/Better Auth 等实质代码，但 M1 的运维脚本存在失联依赖，完整 Compose 的数据库迁移命令指向不存在的文件，直接调用迁移实现也因版本清单过期而拒绝启动；S5 启动/profile 没接入部署。更深一层，Qdrant、push、STT 和出站策略并未形成文档承诺的统一运行时边界。应先修可启动性和身份/供应商边界，再做产品闭环验收，不能仅把镜像入口改成 `fork.main:app` 就称为完成。

## 1. 范围与证据等级

本路负责 `deploy/self-host/`、`backend/fork/`、`backend/firestore_pg/`、`auth-server/` 以及它们实际调用的后端代码；查阅 `AGENTS.md`、`AGENTS.fork.md`、两个 backend guide 和 `dev/unified-main/{02,03,07}*.md`。本轮不改产品代码、不发布生产、不修改外部用户状态。

| 证据 | 本轮结论 | 能证明什么 |
|---|---|---|
| 当前 checkout 与代码追踪 | 已完成 | 文件、入口、调用链、配置消费者是否存在 |
| Compose 配置解析 | PASS | YAML/插值可解析；不证明命令文件、镜像依赖或服务能启动 |
| 运维/零供应商 self-check | FAIL | 当前主线自身缺验收依赖，失败发生在真实部署前 |
| 选定后端测试 | 25 tests PASS；1 test file collection error | 注册表、profile、speaker dispatch 的既有窄契约；不证明完整 API 路径 |
| 真实 JWT 校验与 Python worker 启动探针 | 发现两个实质错误 | 错误 issuer/audience 被接受；sitecustomize 报错后进程仍继续 |
| 隔离 Auth + PostgreSQL 部署 | 见第 4 节最终记录 | 只对该子栈作局部结论 |
| 当前主线完整产品闭环/标准 Linux 机器部署 | NOT PROVEN | 未跑通登录→录音→对话→记忆→导出；Docker Desktop 也不等于原生 Linux 验收 |

原始日志：`/tmp/memweft-audit-20260904/server/`。日志不含真实用户数据或服务密钥；本地测试使用临时随机凭据和 `example.invalid` 合成身份。通用 preflight/sync 日志由主审计保存在 `/tmp/memweft-audit-20260904/common/`。

## 2. 当前真实链路与阻塞

### 2.1 运维入口先于服务启动失败（P0，SH-1）

- `deploy/self-host/operations.sh:15,385-389` 要求 `.github/scripts/check_self_host_deployment.py`，该文件在当前 SHA 不存在。`operations.sh self-check` 实测 exit 1，甚至没有解释性输出。
- `deploy/self-host/zero-vendor-acceptance.sh:10,31-33,51-57` 还要求 `backend/scripts/source_write_freeze.py`、`agent_vm_reconcile.py`、`test-agent-vm-reconcile.sh`，四项都不在当前主线；`--self-check` 实测在第一个缺失文件停止。
- `compose.production.yml:128-144` 的一次性 `firestore-pg-migrate` 调 `python scripts/firestore_pg_migrate.py migrate`；这个文件不存在。`backend` 与 `queue-worker` 又要求该迁移成功（同文件 `338-339,415-416`）。实际执行等价命令 exit 2，报 `No such file or directory`。
- `backend/firestore_pg/migrations.py:337,404` 的迁移/校验实现存在，但直接在隔离PostgreSQL上跑事务集成测试，22例均在fixture调用 `migrate()` 时失败。`migrations.py:209-219` 拒绝未版本化的 `chat_first_dead_letters`、`conversation_keyframe_jobs`、`frame_requests` 三个collection。恢复CLI以后仍需补新的schema migration/version，不能修改已冻结版本或跳过库存校验。

来源是已经合入的 [M1 PR #7](https://github.com/summersmile1984/omi/pull/7)，因此新增的启动/命令闭包守卫有真实实例依据。应在 fork 自有 manifest 的现有 local/CI lane 接入，而非再增加无人调用的检查脚本。

### 2.2 镜像、profile 与 worker 的权威不一致（P0，SH-1）

- 三个 Python 服务都构建 `backend/Dockerfile`（Compose `132,186,370`）；Dockerfile `54` 仍启动 `uvicorn main:app`。计划中的 `deploy/self-host/Dockerfile` 与 `backend/requirements-fork.txt` 尚不存在。
- Compose `197,382` 写 `OMI_DEPLOYMENT_PROFILE=self_hosted`；`backend/fork/profile.py:44-54,64-80` 要求完整行名，如 `self_hosted.production`。当前 `deployment_profiles.generated.json` 只有 `omi_cloud` 的行。短名和完整 self_hosted 名均不能从该表解析；实测为 `ProfileError`。生成器 `--target self_hosted --brand omi-upstream --emit-json` 本身可成功，说明输入源有了，构建消费未闭环。
- `backend/fork/patches/__init__.py:19` 仅收集 storage、queue、speaker 三类 patch；PG/Auth 等仍依靠旧文件中的环境分支。不能假设所有 fork 行为由一个 profile 掌控。
- Compose 的 worker 未设置 `PYTHONPATH=.../fork`，未应用 `sitecustomize`。更重要的是，**即使配置它也不能保证失败退出**：用当前 `sitecustomize.py` 和不存在的 profile 启动 Python，日志打印 `ProfileError` 后仍执行测试 marker，最终 exit 0。CPython 会处理普通 `sitecustomize` 导入异常；在文件中 `raise` 并不足以成为 worker 启动门禁。且该文件从 `fork.main` 导入 bootstrap，会把 ASGI app 导入链带到非 ASGI 进程。
- `backend/Dockerfile` 未声明 Compose 传入的 `OMI_SOURCE_GIT_COMMIT/TREE` build args 或相应 label；Auth Dockerfile 则有。M1 README 所称三个镜像绑定源 commit/tree，需要重新接通，不能仅靠 Compose 的 label 声明。

### 2.3 数据面：基础适配存在，向量检索尚无 Qdrant 路径（P0/P1，SH-2）

| 子系统 | 已存在的生产接线 | 尚待证明/修复 |
|---|---|---|
| PostgreSQL | `backend/database/_client.py:111-117,159-165` 的两种 client factory 读取 `FIRESTORE_PG_DSN`；`firestore_pg/` 有迁移、transaction、collection 管理 | 发布迁移 CLI 丢失且现有migration inventory过期，22个事务例均未进入测试主体；完整 API 中客户/计算数据均经 PG 的行为验收、崩溃恢复与升级校验尚未完成 |
| MinIO | `backend/utils/other/storage.py:64-71` 可转 `fork.storage_minio`，S5 也有 factory patch | 同一行为被环境分支和 patch 双重控制；需用户隔离、加密、签名下载、删除、权限错误和对象往返验证 |
| Redis queue | `backend/utils/cloud_tasks.py:289-290,320-321,342-343,377-378` 已转 `utils.cloud_tasks_redis` | worker secret/重投递/幂等/中断恢复必须实际执行；当前 healthcheck 仅 `kill -0 1`（Compose `423` 附近），不能证明消费正常 |
| Qdrant | Compose 提供 Qdrant 服务、`VECTOR_STORE_PROVIDER=qdrant` 和 `QDRANT_URL` | 在 backend 非测试 Python 全量搜索中两个变量均无消费者；`backend/database/vector_db.py:116-127` 仍建 Pinecone client/index，无 Qdrant adapter。没有 Pinecone key 时 `index=None` 也不意味着 Qdrant 已接上 |

不能把基础容器全部 healthy 等同“对话/记忆搜索可用”。数据面验收至少需要同一用户写入→检索→更新→删除、另一用户不可读，以及队列故障重试后不重复产生业务副作用。

### 2.4 身份：正向登录已有，JWT 部署边界校验缺失（P0，共享 AUTH-1）

- `auth-server/src/auth.js:127-141` 配置 ES256、issuer、audience、TTL/轮换；生产配置检查和开发签发桥禁用已有（`38-69`）。
- `backend/utils/other/endpoints.py:130-143` 将 `AUTH_PROVIDER=better_auth` 请求交给 `utils.auth_shim.verify_id_token`。该 verifier 允许 ES256/RS256/EdDSA，并可按 kid 刷新 JWKS，但在 `backend/utils/auth_shim.py:126-131` 明确 `verify_aud=False`，不校验 `AUTH_JWT_ISSUER`；注释“Better Auth JWT carries no aud”也和当前 Auth server 配置相反。
- **实测**：临时生成 ES256 key，唯一替换 `_fetch_jwks` 为本地公钥来源，真正调用当前生产 verifier。正确 issuer/audience、错误 issuer/audience、缺失 issuer/audience 三种都得到 `ACCEPTED audit-user`。没有访问任何外部系统。
- `deploy/self-host/auth-flow-smoke.py:115-132` 虽接收/设置 issuer 和 audience，却只断言 uid/sub；因此正向 smoke 通过不能证明租户/部署之间的令牌边界。
- `auth_shim.py:78-84` 还存在无限期 stale JWKS fallback、异常文本携带上游响应的风险；AUTH-1 应在同一个密钥轮换/失效模型中定义时限与脱敏，按仓库现有 fallback telemetry 记录，避免各 target 自己发明不同策略。

JWT签名/JWKS、issuer、audience、expiry、轮换是一层；sessionGeneration、会话撤销与账户删除权威是第二层。CF改为本地JWT校验时不得丢失其已有第二层校验，两个target都要对同一撤销/删除负例给出等价结果。不能仅把内部verify HTTP调用替换成JWT库就算完成。

本路是 **AUTH-1 唯一主责**：共享身份形状/claims/轮换/会话行为归 `auth/shared/`；CF 路实现 D1/Hono adapter 与 Edge verifier 对齐；白牌路通过 CLIENT-1 消费登录/回调/刷新契约。

### 2.5 模型、推送与出站：部署变量不是功能实现（P0，SH-3）

- `deploy/profiles/self_hosted.yaml:21-25` 声明 push=webhook、tts=mimo、STT operator_mimo/operator_moss/sensevoice_local；Compose/示例却默认 push=disabled、tts=sherpa_onnx、prerecorded=mlx_moss_diarize。两份“权威”互相矛盾。
- 当前后端生产 Python 没有 `PUSH_PROVIDER` 的消费者。`backend/utils/notifications.py:8,29-30,148-150` 仍直接使用 Firebase auth/messaging；不能认可 README 的“disabled 会阻止 token 读取/FCM 初始化/后台投递”声明，也不能给白牌端展示推送可用。
- `backend/utils/stt/pre_recorded.py:83-124,1026-1038` 实际只选择 MOSS、Modulate、Parakeet；Compose 的 `mlx_moss_diarize` 不被 selector/provider factory 识别。fork 的枚举/config 单元测试只证明常量和配置存在，不能证明生产 STT 会调用它。
- SenseVoice socket 需要 `sherpa_onnx`（`backend/utils/sensevoice/socket.py:58-69`），该包不在当前 backend runtime lock，且没有 fork 依赖层。`_sensevoice_available()`（`streaming.py:686-690`）只看 model 文件，不证明 tokens/运行时依赖齐全。
- 出站 guard 在 `backend/fork/egress_policy.py`，部分 MiMo/SenseVoice/speaker 路径有直接调用，但共享 HTTP client factory 未接该 hook。更确定的错配是 guard `25,98-99` 只识别短名 self_hosted/neutral；**实测** `self_hosted` 拒绝 `https://api.openai.com/v1/models`，同一 guard 在 `self_hosted.production` 下返回 ALLOWED。探针只做 URL 校验，没有出站请求。

SH-3 应由已解析的同一 profile 决定供应商及能力，先确保未配置能力以契约规定的 unavailable 失败，再接实际运营方模型。不要为了让 CI 绿而换回上游模型，也不要把测试 fixture 宣称为真模型语音验收。

### 2.6 Web 的计划文字已落后于 main（共享 WEB-1）

`dev/unified-main/03-deploy-targets.md` 仍写“保留 Next standalone，不引入 Bun”；但当前 `web/app/package.json:6-14` 使用 Moonshine/Bun，`web/app/Dockerfile:1` 是 `oven/bun:1.3.14-alpine`。标准 Compose 没有 web 服务，也没有正式生产反向代理服务，只有 cutover 测试 overlay。

因此 Server 路应配合 **CF 主责的 WEB-1**：以当前上游 Web 源码为基础提供标准 OS 打包与 CF 打包，不由本路重新引入 Next 迁移或另建长期 Web 分支。客户端 API/auth origins、同源代理、cookie、首帧鉴权和回调由 CLIENT-1/AUTH-1 定义共同边界。

## 3. 本轮命令与结果

除注明外均在上述审计工作树执行。`PY` 使用主审计准备的 backend 锁定测试环境 `/Users/macstudio/Documents/memweft-worktrees/three-track-action-plan/backend/.venv/bin/python`；注册表最初的直接 unittest 使用现有 S5 venv。未复制任何旧分支源码作为测试目标。

| 命令/探针 | exit/结果 | 日志与覆盖边界 |
|---|---|---|
| `bash deploy/self-host/operations.sh self-check` | 1 | `operations-self-check.log`；缺配置 checker |
| `PYTHON=python3.11 bash deploy/self-host/zero-vendor-acceptance.sh --self-check` | 1 | `zero-vendor-self-check.log`；第一个缺依赖即停止，产品 loop 未运行 |
| `bash deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production.example deploy/self-host/compose.production.yml --project-name memweft-audit-server-20260904 config --quiet` | 0 | `compose-config.log`；只解析示例，未用示例启动或访问外部服务 |
| `OMI_DEPLOYMENT_PROFILE=self_hosted PYTHONPATH=backend python3.11 -c 'from fork.profile import current; print(current())'` | 1 | `profile-selection.log`；未知 row/错误生成目标 |
| `python3.11 scripts/profiles/render.py --target self_hosted --brand omi-upstream --emit-json` | 0 | `selfhost-profile-render.log`；仅输出，无改生成文件 |
| 从 backend 执行 `python3.11 scripts/firestore_pg_migrate.py migrate` | 2 | `missing-migration-command.log`；命令文件缺失 |
| `PYTHONPATH=backend python3.11 -m unittest fork.tests.test_patch_registry fork.tests.test_profile_selection -v` | 0，13 PASS | `focused-registry.log`；测试抽象注册表和人工 profile table |
| 直接 `unittest ...test_real_seams` / `discover -s fork/tests` | 1 | `real-seams.log`、`fork-all.log`；直接调用缺 ENCRYPTION_SECRET，随后改正式 runner复核，不能把环境错配归产品问题 |
| `PYTHON=$PY BACKEND_UNIT_TEST_FILE_LIST=/tmp/memweft-audit-20260904/server/fork-tests.txt BACKEND_PYTEST_WORKERS=3 bash backend/test.sh` | 1；五文件共25 PASS，另1文件 collection error | `backend-fork-runner.log`；错误为迁移后的 exporter test 仍 import 已无的 `scripts.export_cloudflare_x_posts`；fork manifest 默认仅选3文件，漏掉此错误 |
| 临时 ES256/JWKS contract probe | 0；三种 claims 全接受 | `jwt-contract-probe.{py,log}`；0表示探针运行完成，错误/缺失 claims 被接受是**产品失败** |
| 子进程加载真实 `sitecustomize`，坏 profile | 0；报错后 worker marker继续 | `sitecustomize-fatality.log`；证明普通导入异常未阻止工作负载 |
| 真实 `assert_http_endpoint_allowed`，短/长 profile | 0；短名 REJECTED、长名 ALLOWED | `egress-profile-probe.{py,log}`；只验证 URL，不访问外网 |
| 非测试 Python 全量配置消费者清单 | 完成 | `runtime-config-consumers.json`；负向搜索结合真实调用链使用，不能单独当行为测试 |

通用门禁要区分两个范围：

- `scripts/fork/preflight --base origin/main` exit 0，但审计分支与基线无差异，files=0，只选 11 个上游检查+1 个 fork 检查；failure-class 历史检查因 shallow 明确 SKIP。这不是当前产品整体通过。
- 累计 `--base fd01c27267` 选 50 项/898 changed files；前 4 项通过，第 5 个 file-line-count ratchet 因 4 文件失败而停止，当时其余 45 项及后续 fork 阶段未执行。随后主审计独立补跑其中 6 项全部通过，仍有 39 项未取得累计范围执行结果。它揭示规则/累积变更负担，不能被解释成 4 个业务功能故障。
- 6项补跑为 runtime-image-source-closure（13个已注册镜像）、import-purity（69生产文件）、module-isolation（13测试文件）、production-data-plane-routing（12 tests）、deployment-secret-boundary、desktop-auth-session-ratchet。13镜像source closure通过并不覆盖Compose引用的缺失迁移CLI或Qdrant/STT/push能力；CI-1应以本次已合并M1真实缺口扩展现有门禁。
- 统一 upstream-touch 累计审计发现 38 个违规文件；本路下一批必须继续用 fork 自有文件/构建层修复，不能重新搬回旧分支的大量 upstream edits。

## 4. 隔离部署验收记录

本机 Docker client/server 均为 29.4.2。已有 `omi-self-host-smoke-*` 容器 healthy，但 backend 的 Compose working-dir label 指向旧 `deployment-model-neutrality` 工作树，未提供当前 main 的源 commit label；**没有停止、重启或复用该数据栈，也没有把它记为当前 main 通过**。

本轮创建单独 `memweft-audit-server-20260904` 项目：main 的 `auth-server/Dockerfile` 构建成单独 tag，PostgreSQL 临时数据，只有 `127.0.0.1:33047`（Auth）和 `127.0.0.1:55437`（PG）。使用合成账户、随机凭据、生产模式 Auth 设置。这个缩小范围是为了定位“哪一部分确实可工作”，并不替代完整 Server 目标验收。

| 实际操作 | 结果 | 证据与限制 |
|---|---|---|
| `docker compose -f <临时auth-pg配置> up --build --wait` | exit 0 | `auth-pg-up.log`；main Auth Dockerfile用锁定依赖全新构建，postgres/auth均healthy，auth-migrate成功。不是正式11服务Compose。 |
| `auth-server node src/migrate.js --check` | exit 0，无pending migrations | `auth-migration-check.log`；只证明Better Auth schema/JWKS，不能证明firestore_pg schema。 |
| 现有 `auth-flow-smoke.py --base-url http://127.0.0.1:33047 --issuer https://auth.audit.example.invalid --audience https://api.audit.example.invalid --origin https://app.audit.example.invalid`（admin secret走环境） | exit 0 | `auth-flow-live.log`；注册、登录、session bearer、JWT、真实JWKS、当前backend verifier、账户/会话删除残留校验通过。issuer/audience负例仍失败，见前述独立生产函数探针。 |
| `FIRESTORE_PG_DSN=<独立PG> $PY -m pytest firestore_pg/tests/test_transaction_semantics.py -q`（backend目录） | exit 1，22 setup errors | `pg-transactions-live.log`；每例fixture调用生产 `migrate()`，统一因3个collection缺版本化迁移而拒绝。事务本身、冲突重试、跨用户删除均**未被执行/证明**。 |
| Auth镜像源码label | commit和tree吻合 | `auth-image-source.log`：commit=`d238a85af9d999992d9f0352db682cd9f11fc951`，tree=`9e2248ef808bb73083214da4b8b1cab8e897328d`。 |
| 清理本次专用Compose项目 | exit 0 | `audit-stack-cleanup.log`；仅移除本次临时容器/network/临时数据，临时明文凭据删除，保留 `auth-pg.compose.redacted.json`。扫描审计日志中随机凭据匹配数为0。 |

因此可给 **Auth + PG身份子栈局部PASS**，并明确 **firestore_pg迁移NO-GO、完整Server目标NO-GO**。本轮没有通过旧栈数据或源码改动绕过失败。

完整目标仍缺：按正式 Compose 启动后端/queue、Web 浏览器登录、真实语音录制与转录、对话落库与记忆检索、导出/删除、推送 unavailable、无上游供应商出站、重启恢复/备份恢复、原生标准 Linux 部署。按当前代码证据，不能把这些项目标成 PASS。

## 5. 按依赖安排的可独立验证 PR 行动包

这些行动包都以短分支从 `main` 开工、同一主线合入。部署 target/brand 是目录与构建参数，禁止再创建长期 target 分支。包的单位是可验证行为边界，不是逐个常量拆 PR。`CLIENT-1` 的Web消费者、`CF-2`和`WEB-1`作为 **INTEGRATION-1** 在同一短期集成PR候选树联合验收；本路先提供WL-1确定输入下的AUTH-1测试服务与可执行参考后端，不等待CLIENT-1最终交付后才提供服务。三部分不互相等待先合并，双target Web登录/实时闭环是联合批次的合入门禁。

| 顺序/包 | 主责与范围 | 依赖 | 主要错误路径与完成门禁 | 产物 |
|---|---|---|---|---|
| **P0 SH-1 可靠启动与 profile 权威** | Server；fork 镜像依赖层/可归因镜像、API/worker 入口、PG迁移CLI、配置校验与运维脚本缺依赖清理；只有一个 resolved profile | 无；profile schema与CLIENT-1协同 | 正确 profile 真正启动；错误 brand/target、缺表、缺依赖、失效patch在 API和worker子进程均非0退出；为3个新collection补向前迁移，PG首次/重复迁移成功、失败阻止API启动；三种服务映像可查到源码身份 | 可启动最小 CI Compose、启动命令闭包测试、fork manifest local/CI接线、更新运维指南 |
| **P0 AUTH-1 共享身份契约** | **Server唯一owner**；`auth/shared/` 公共参数/claims/会话/轮换契约，PG auth adapter、backend verifier；CF实现D1/Hono与Edge adapter，保留sessionGeneration/撤销/账户删除的权威校验 | 与SH-1并行；CLIENT-1消费同一规格 | 真服务注册→登录→JWT→受保护API→过期/刷新→注销/删除；错issuer/aud、缺claims、未知kid、坏算法、过期和已撤销会话拒绝；legacy principal和轮换重叠期按明确兼容契约测试；生产无dev issuer | `contracts/auth/` 同一套夹具/执行器、共享TS包、两target verifier测试、更新auth smoke |
| **P0 SH-2 核心数据面闭环** | Server；PG/MinIO/Redis已有adapter梳理、补Qdrant与检索owner；API/worker使用同一profile和UID边界 | SH-1；API身份集成最终依赖AUTH-1 | 两用户隔离；对话/记忆创建、搜索、删除；对象签名往返；队列重投递/中断恢复不重复副作用；错误worker secret拒绝；缺Qdrant/PG/Redis得到准确失败，不进入云客户端 | 实际数据面集成用例、明确readiness/worker消费健康、fresh/upgrade/restore的迁移证据 |
| **P0 SH-3 模型与可选能力边界** | Server；STT selector+adapter+runtime lock、TTS/实时/embeddings、push禁用/启用门禁、HTTP出站guard；收敛profile与Compose冲突 | SH-1；存储类闭环依赖SH-2；共享能力schema联动CLIENT-1 | 缺模型/密钥、坏端点、未知provider、所有fallback均不能调用Omi/官方供应商；长profile名保持拒绝；disabled push在HTTP与后台均无FCM操作；配置的真实音频必须经选定STT产生内容；测试provider仅用于CI | provider能力矩阵、fork依赖锁/模型hash契约、无网络fault tests、真语音局部验收报告 |
| **P1 WEB-1 双target Web打包** | **CF主责**；Server提供当前Moonshine/Bun的OS镜像/Compose、反向代理、同源auth/API与WS升级 | CLIENT-1、AUTH-1；Server启动依赖SH-1 | 冷浏览器登录→刷新→重连；首帧JWT正确/错误；cookie/redirect行为一致；构建不嵌服务端secret；无目标专属客户端分叉 | 同一Web源码的两种可重复构建、Server Web部署入口 |
| **P1 CI-1 共同产品契约与发布验收** | 双target分别接相同 `contracts/`；Server提供隔离Compose runner；白牌路在其上加brand×platform矩阵 | SH-1/2/3、AUTH-1、CLIENT-1、WEB-1 | 干净clone和空数据启动；登录→录音上传→转录→对话→记忆→检索→导出→删除；另一用户不可访问；断Redis/失效JWT/模型失败；两target同一API+WS夹具；CI只用hermetic provider | fork checks扩展、可追溯E2E报告、两target语义差异清单归零或由能力契约明确 |
| **P1 SH-4 运维与候选发布** | Server；标准Linux首次安装/升级、HTTPS、备份恢复、镜像归因/签名、健康/队列观测；修复旧zero-vendor runner并消除文档虚假承诺 | 前述产品验收完成；白牌构建产物 | 隔离主机恢复演练后重新跑Auth/数据/检索；实际模型端点+音频验收；规则失效不能授权cutover；操作员外部证明缺失时明确NOT_PROVEN | Linux安装包/Compose发行目录、环境/秘密清单、恢复报告、candidate manifest及发布工作流 |

SH-2 中新增 Qdrant 适配应遵守上游向量契约，在 fork 所有的 typed adapter/注册表中完成；不得以“先保留 Pinecone”作为自托管完成条件。SH-3 的 provider 列表应由现有部署意图收敛为一个版本化契约；可选功能暂未配置时应准确声明不可用，而不能依靠错误配置静默回退。

AUTH-1 和 SH-4 涉及访问控制/迁移/发布；实现与本地验证可以立即做，但远端发布、数据迁移、合并仍遵循仓库要求的显式授权，不把这份行动方案理解为生产变更指令。

## 6. 当前即可开工的第一批

1. **SH-1**：修复运维依赖/迁移命令、独立 bootstrap/worker 可失败启动、生成表进入镜像；同时给首次部署和坏配置增加真实子进程验证。无需等待品牌名或生产凭据。
2. **AUTH-1**：固定现有身份契约来源和错误 claims 测试，修复 issuer/audience/required claims；抽共享参数/轮换逻辑，CF adapter和客户端同时按一个owner的规格接线。先用合成品牌与临时数据库完成。
3. **SH-2 的可控数据面工作**：整理PG/MinIO/Redis生产接线与测试、Qdrant adapter和跨用户隔离；标准端口/对象桶均可用隔离fixture验证，不需运营方域名。
4. **SH-3 的边界守卫**：完整profile名的出站策略、禁用push、未知STT选择必须失败，补fork依赖和生产selector测试。真实模型质量验收可在上述边界正确后使用运营方资源，但不能把它记为已完成。
5. 与 CF/白牌同步 **WEB-1/CLIENT-1/CI-1** 的输入输出；本路不新建第二套客户端profile或auth规格。

第一批完成后应能交付“当前 main 的隔离后端可启动，坏配置不会运行，JWT与会话边界正确，核心存储可往返”的证据。只有随后共同产品路径和白牌安装包都通过，才能交付用户所要的“单 main、双 target、白牌终端”版本。
