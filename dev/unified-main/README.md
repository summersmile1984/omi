# 统一主线执行方案：一条 `main`、两个部署目标、N 个品牌

> 日期：2026-09-02 · 状态：待决策签字（`07-pr-plan.md` §1）后开工
> 前置研究：`omi-repo-topology.md`（为什么是单主线）、`omi-white-label-strategy.md`（品牌触点全清单）、`omi-cloud-neutral-postgres-migration.md`、`dev/cloudflare-adaptation-plan.md`、`dev/cloud-neutral-overview.md`。本目录是"怎么做"，那些文档是"为什么"。

## 一句话

把 `codex/cloudflare-adaptation` 与 `feature/cloud-neutral-shim` 各自的**接缝**（客户端认证、部署 profile、Web 构建、限流/检查清单）在 `main` 上重写成一份，把它们的**新增目录**（`deploy/cloudflare/` 616 文件、`deploy/self-host/` 24 文件、`backend/firestore_pg/`、`auth-server/`）直接检出，把**不该合的**（Moonshine 重写、上游文件格式化、上游测试改动、上游不变量改动）归档；之后部署目标由 `deploy/<target>/` + profile 表达，品牌由 `brand/<id>/` 表达，CI 跑 品牌 × 目标 矩阵，上游每周合一次、真实冲突 ≤5 个文件；**上游文件默认零改动**，例外只在 T1 白名单里（`00`）；契约权威是上游 API 与自托管参考实现，Cloudflare 单向对齐；Web 保持上游 Next.js，不引入 Bun。

## 文档索引（按执行顺序读）

| 文档 | 回答的问题 | 产出物 |
|---|---|---|
| [00-upstream-touch-policy.md](00-upstream-touch-policy.md) | 为什么"能不改上游代码就不改"、shim 分支 653 个上游文件改动的诊断、T0 技术目录（每个平台）、T1 白名单、T2 禁改、两条测试通道 | 纪律 + 技术目录 |
| [01-branch-consolidation.md](01-branch-consolidation.md) | 两条分支怎么收敛到 main：冻结、先同步上游、接缝 PR（S 系列）、新增目录合入（M 系列）、20 个冲突文件归属、门禁命令、回滚 | 操作手册 |
| [02-deployment-profile.md](02-deployment-profile.md) | 客户端与后端如何用同一份 profile 同时支持 `omi_cloud / self_hosted / cloudflare`；身份契约 v1（Better Auth 两种部署同一契约）；能力开关默认值；两分支现有代码的迁移映射 | 设计 + 生成器规范 |
| [03-deploy-targets.md](03-deploy-targets.md) | `deploy/self-host/` 与 `deploy/cloudflare/` 各自的目录契约、合入时的调整、混合部署、共享服务端资产、Web 运行时决策 D3、Cloudflare 未移植清单 | 目录契约 |
| [04-brand-layer.md](04-brand-layer.md) | 白牌层怎么落地：`brand/manifest.yaml` schema、`apply.py`/`check.py` 规范、每平台生成物、B0–B8 PR 拆分、契约组、私有 overlay | 实施规范 |
| [05-ci-matrix.md](05-ci-matrix.md) | 上游工作流如何处置（禁用脚本）、fork 检查清单与本地入口、`deploy/matrix.json`、fork 工作流一览、密钥门、标签命名空间 | CI 设计 + 骨架 |
| [06-upstream-sync.md](06-upstream-sync.md) | 每周同步流程、13 个现存冲突的永久处置、"不修改上游文件"清单、按类别的冲突处置规则、度量与自动化 | Runbook |
| [07-pr-plan.md](07-pr-plan.md) | 决策登记 D1–D11、S/M/C/B 四个系列的 PR 表（依赖、人日、验收证据）、10 周排期、PR 模板、执行期风险 | 计划 |
| [upstream-touch-allowlist.yaml](upstream-touch-allowlist.yaml) | 唯一允许被 fork 修改的上游文件清单（T1，逐条限行数）+ 永不可改的 T2 模式 | 守卫数据 |
| [upstream-prs.md](upstream-prs.md) | 回推上游的 PR 队列；每被接受一个就删一条白名单 | 队列 |
| [templates/sync-pr-body.md](templates/sync-pr-body.md) | 同步 PR 的描述模板 | 模板 |

## 目标形态

```
main（fork of BasedHardware/omi）
├── app/ desktop/ web/            # 上游客户端（默认零改动）；Firebase 等通过包/模块别名换成 fork shim；少数 T1 钩子在白名单
├── backend/                      # 上游单体（零改动）+ backend/fork/（入口、补丁注册表、provider、shim、tests）+ firestore_pg/
├── auth/shared/  auth-server/    # Better Auth 共享逻辑 + 自托管 adapter（Cloudflare adapter 在 deploy/cloudflare/workers/auth）
├── contracts/                    # 上游 parity 夹具 + fork 新增 auth/realtime/api-smoke 套件，对两个后端都跑
├── deploy/
│   ├── profiles/                 # 部署 profile 单一事实源 → 四端生成表
│   ├── matrix.json               # 品牌 × 目标 × 组件
│   ├── self-host/                # compose、验收、发布
│   └── cloudflare/               # Workers、迁移、清单、发布
├── brand/                        # 品牌清单与资产（可放私有 overlay）
├── scripts/{brand,profiles,fork}/ # apply/check/render/preflight/upstream-touch
├── .github/checks-manifest.fork.yaml  +  .github/workflows/fork-*.yml
└── AGENTS.fork.md（及各组件 *.fork.md）  # fork 纪律；上游 AGENTS.md 只加一行指针
```

## 执行顺序与里程碑

1. **决策**（W1）：D1–D11 签字；品牌名商标检索启动。
2. **S 系列**（W1–W4）：S0 上游同步并消除 13 个冲突源 → S1 profile 源 → S2/S3/S5 并行 → S4/S6。里程碑 W3：`main` 以 `self_hosted` 端到端可用。
3. **M 系列**（W3–W6）：M1 自托管余量 → M2 Cloudflare 目录与契约对齐 → M3 矩阵观察 → M4 删分支。里程碑 W5：两目标契约在 `main` 全绿，长期分支冻结。
4. **C 系列**（W1–W9，穿插）：C0/C1 最早（禁用上游工作流、fork 清单），C5 发布工作流最后。
5. **B 系列**（W6–W9）：白牌层；里程碑 W8：`check.py` 零泄漏。
6. **收尾**（W10）：全链路 E2E、一次上游同步演练（≤半天、冲突 ≤5）、两目标各一次 staging 发布。

## 验收总表（全部满足才算"合并成一个 main"完成）

- [ ] `git branch -r` 只有 `origin/main` 与短期分支；两条旧分支只以 `archive/*` tag 存在。
- [ ] `git merge-tree --write-tree main upstream/main | grep -c '^CONFLICT'` ≤ 5，并连续两次周同步保持。
- [ ] `scripts/fork/check-upstream-touch.py` 报告被 fork 修改的上游文件数 = T1 白名单条目数（≤12），其中 `backend/**`、上游测试、锁文件、生成文件、CI 为 0。
- [ ] 上游组件测试在"上游模式"（无 shim env）全绿，且上游测试文件未被修改。
- [ ] `scripts/profiles/check_tables.py` 通过；`render.py --target omi_cloud` 与上游字面量等价。
- [ ] 同一 Flutter/macOS/Windows/Web 源码，仅切换 profile 即可对 `deploy/self-host` 与 `deploy/cloudflare` 两个 staging 完成登录→录音→对话→记忆→导出闭环。
- [ ] `fork-contract-selfhost.yml` 与 `fork-contract-cloudflare.yml` 跑**同一套** `contracts/` 套件且全绿。
- [ ] `fork-build-matrix.yml` 在无密钥的 fork PR 上全绿；有密钥时对每个 品牌 × 客户端 产出可安装件。
- [ ] `apply.py --brand omi-upstream --check-clean` 零 diff；`check.py --brand <brand>` 三个面为零。
- [ ] 上游 `openapi-contract.yml` 在 `omi-upstream` 品牌下输出与上游字节一致。
- [ ] 每周自动同步 PR 由 `fork-upstream-sync.yml` 生成，`sync-log.md` 有连续记录。

## 使用方式

- 新人：读本页 → `00` → `02` → `06`，即可在 `main` 上安全做事。
- 开一个功能 PR：先 `make preflight && scripts/fork/preflight`；**不要修改上游文件**——需要改上游行为时查 `00 §3` 的 T0 技术目录，实在不行才申请 T1 白名单条目并同时提上游 PR。
- 加一个品牌：`brand/<id>/manifest.yaml` + `deploy/matrix.json` 一行 → `apply.py` → PR。
- 加一个部署目标：`deploy/profiles/<target>.yaml` + `deploy/<target>/` 目录 + 契约工作流；客户端不改。
