# CI 索引与覆盖矩阵

> 日期：2026-09-22 · 作者：fork 自有维护；上游 GitHub 不可见。
> 入口：`AGENTS.fork.md` §7。改任何 fork CI 命令、删除任何 `deploy/**/ci/*`、`contracts/deployment/**`、
> 改 manifest id 或 stage 矩阵前，必须先回到这份文档同步再提交。

## 1. 三阶段流水线

```
阶段 1: dev/local.sh up              阶段 2: fork-checks.yml       阶段 3: fork-release-prepare.yml
阶段 1: dev/local.sh verify          → fork-cd-cloudflare.yml / fork-cd-server.yml
阶段 1 → 阶段 2: scripts/fork/preflight 阶段 2 → 阶段 3: prepare → qualify → ready
```

完整说明：

| 文档 | 覆盖范围 |
|---|---|
| [`dev/ci-cd-three-stages.md`](ci-cd-three-stages.md) | 三阶段主线：本地 → CI → CD × 2；2026-09-21 已用真实模型跑通，是当前事实。 |
| [`dev/unified-main/05-ci-matrix.md`](05-ci-matrix.md) | fork CI 设计文档（2026-09-04 规划稿）；§4/§5/§8 的矩阵与 C2-C5 已声明**未实现**，实际由 `deploy/profiles/*.yaml` + `brand/*/manifest.yaml` 承担。 |
| [`scripts/fork/README.md`](../../scripts/fork/README.md) | fork CI 总览：哪些 lane 跑、怎么本地 preflight、怎么生成 manifest attestation。 |
| [`scripts/fork/RELEASE.md`](../../scripts/fork/RELEASE.md) | 发布 lane 入口：freeze → qualify → ready → CD；attestation 与 admission 的契约。 |
| [`config/repo-state.fork.json`](../repo-state.fork.json) | workflow 启停、quarantine、required checks、environment 保护——GitHub 设置的声明。 |

## 2. fork manifest 检查矩阵（37 条）

每条检查的 id / 真实命令 / trigger / runner / 本机可跑性。验证命令：

```bash
backend/.venv/bin/python scripts/fork/run_checks.py \
  --lane local --base origin/main --platform linux --list
```

| id | 真实命令 | trigger glob 数 | runner / 平台 | 本机 | 备注 |
|---|---|---|---|---|---|
| fork-cloudflare-product-core | `bash deploy/cloudflare/ci/product.sh` | 8 | linux amd64 / workerd + D1 | 可 | 16 真实 HTTP 合同；本机跑需要 workerd + D1。 |
| fork-selfhost-product-core | `bash deploy/self-host/ci/product.sh` | 13 | linux amd64 + PG/Redis/MinIO/Qdrant + 模型 | 可 | 16 真实 HTTP 合同；需要 3 套模型库或显式 MiMo + BGE-M3。 |
| fork-electron-native-identity | `bash desktop/windows/fork/test.sh` | 7 | linux x86 | 可（受限） | Vitest + Python 单测 + ci_build；不打包、不签名。 |
| fork-selfhost-build-context | `python3 deploy/self-host/ci/build_context.py` | 4 | 任意 | 可 | 离线 scratch Docker build。 |
| fork-flutter-native-identity | `bash app/fork/test.sh` | 11 | macOS（推荐） | 不可（无 macOS） | Flutter 3.44.5 / Dart 3.12.2。 |
| fork-macos-native-identity | `bash desktop/macos/fork/test.sh` | 9 | macOS | 不可（无 macOS） | xcrun swift test。 |
| fork-macos-native-compile | `bash desktop/macos/fork/compile.sh` | 9 | macOS | 不可（无 macOS） | staged debug 编译。 |
| fork-overlay-owner-audit | `python3 scripts/fork/test_overlay_owner_audit.py && python3 scripts/fork/check-overlay-owners.py` | 10 | 任意 | 可 | flutter/macOS/Electron 三方 owner。 |
| fork-ci-diff-base | 8 个 test_* 串接 | 9 | 任意 | 可 | 跑 workflow_dispatch / push / pull_request 真实 fixture。 |
| fork-workflow-lint | `python3 scripts/fork/workflow_lint.py` | 4 | 任意 | 可 | actionlint + fork label 目录。 |
| fork-deployment-settings | `test_deployment_settings.py && check_deployment_settings.py --base {base}` | 3 | 任意 | 可 | 部署密钥边界。 |
| fork-auth-contracts | `bash scripts/fork/auth-contracts.sh` | 15 | 任意 | 可 | Better Auth 共享合同。 |
| fork-web-build-contracts | `bash deploy/web/ci.sh` | 10 | 任意 | 可 | web build contract 10 tests / 88 assertions。 |
| fork-cloudflare-routes | `bash deploy/cloudflare/ci/routes.sh` | 55 | linux | 可（受限） | 真实 FastAPI 路由 + Core/Edge；本机需要 workerd。 |
| fork-cloudflare-staged-owners | `python3 deploy/cloudflare/scripts/check_staged_owner_coverage.py` | 2 | 任意 | 可 | 触发器覆盖与 stager 投影一致。 |
| fork-web-client-contract | `bash web/app/fork/test.sh` | 8 | 任意 | 可 | Web 端 profile/session 边界。 |
| fork-diff-hygiene | `test_diff_hygiene.py && check_diff_hygiene.py --changed-files … --base … --head …` | 1 (`all`) | 任意 | 可 | provenance-aware 空格 + 冲突标记。 |
| fork-upstream-touch | `python3 scripts/fork/check-upstream-touch.py --aggregate` | 1 (`all`) | 任意 | 可 | 必须 `upstream/main` 已 fetch；缺则 exit 2。 |
| fork-upstream-touch-tests | `python3 scripts/fork/test_check_upstream_touch.py` | 3 | 任意 | 可 | 自身拒绝行为。 |
| fork-upstream-sync-plan-tests | `python3 scripts/fork/test_upstream_sync_plan.py` | 3 | 任意 | 可 | 同步 plan + rerere 行为。 |
| fork-release-failure-tests | `python3 scripts/fork/test_release_failure_summary.py` | 4 | 任意 | 可 | release_failure_summary 自身的失败覆盖。 |
| fork-repo-state | `python3 scripts/fork/check_repo_state.py` | 1 (`all`) | 任意 | 可 | 静态 + GITHUB_ACTIONS=true 时再调 live API。 |
| fork-repo-state-tests | `test_check_repo_state.py && test_apply_repo_state.py` | 5 | 任意 | 可 | 声明的拒绝行为。 |
| fork-profile-tables | `python3 scripts/profiles/check_tables.py` | 11 | 任意 | 可 | 四端 profile 表一致。 |
| fork-backend-seams | `BACKEND_UNIT_TEST_FILE_LIST=… bash test.sh` | 4 | 任意 | 可 | 3 个测试文件。 |
| fork-brand-tooling-tests | `python3 scripts/brand/test_brand_tooling.py` | 3 | 任意 | 可 | brand apply/check 测试。 |
| fork-brand-raster | `npm test --prefix scripts/brand/raster` | 1 | 任意 | 可 | brand 资产解码器。 |
| fork-brand-upstream-clean | `python3 scripts/brand/check.py --brand omi-upstream` | 2 | 任意 | 可 | 上游品牌回归守卫。 |
| fork-profile-boundary-tests | `python3 scripts/profiles/test_profiles.py` | 5 | 任意 | 可 | profile 渲染器五项输出。 |
| fork-selfhost-startup | `BACKEND_UNIT_TEST_FILE_LIST=… bash test.sh` | 20 | 任意 | 可 | 26 个 fork backend 测试文件。 |
| fork-local-dev-harness | `test_local_sh.py && test_selfhost_local_sh.py && test_git_hygiene.py` | 2 | 任意 | 可 | 本地 harness 行为。 |
| fork-backend-test-collection | `pytest fork/tests --collect-only -q` | 3 | 任意 | 可 | 全部 fork 测试文件可导入。 |
| fork-hosted-operator-smoke | `python3 ../deploy/self-host/hosted-live-smoke.py --self-check` | 6 | 任意 | 可 | hosted 厂商调用 builder 自检。 |
| fork-local-dev-harness-e2e | `test_run_e2e.py && run_e2e.py -q --tb=line` | 6 | 任意 | 可 | 真实 upstream E2E hermetic 跑。 |
| fork-container-contracts | `python3 scripts/fork/run-container-tests.py` | 10 | linux + Docker | 可 | PG shadow + Redis 锁场景。 |
| **fork-repo-state-apply** | `python3 scripts/fork/apply_repo_state.py --plan-only` | 5 | 任意 | 可 | **新增**：planner 离线 smoke；不调网络。 |
| **fork-staged-targets** | `bash -c '… --help …'`（4 个 entry point） | 5 | 任意 | 可 | **新增**：stage/brand/flag 入口契约薄守卫。 |

`平台 (platforms)` 字段：

- `platforms: ["macos"]`：`fork-macos-native-identity`、`fork-macos-native-compile`。`run_checks.py --platform linux` 会跳过这两个，必须走 `fork-checks.yml` 的 `fork-macos` job。
- `platforms: []`（默认）：所有平台都跑。
- 没有 `platforms: ["windows"]`：`fork-electron-native-identity` 在所有 runner 上跑；Windows DPAPI 路径不在 CI 覆盖。

## 3. Workflow 与 manifest 的连线

`.github/workflows/fork-checks.yml` 的执行顺序：

```
checkout → install uv (retry) → Provision Python 3.12
→ Fetch upstream/main → Resolve diff base
→ Validate fork ownership (fork-upstream-touch)
→ Provision backend env → Select platform checks
→ Provision go (actionlint) → auth contracts deps
→ Provision Node 22 / Bun 1.3.14 → web contract deps
→ Select native terminal deps → provision Flutter / Electron / brand raster
→ Provision Server model stores (3 个 prepare-*.py)
→ Fork manifest (ci lane) = scripts/fork/run_checks.py --platform linux
→ Upload self-host fixture report
→ Report upstream divergence
→ Upload manifest attestation (FORK_FULL_CHECKS only)
→ fork-macos job (平台独占) = scripts/fork/run_checks.py --platform macos --exclusive-platform
```

`.github/workflows/fork-release-prepare.yml` 与两条 CD workflow：

| workflow | 入口 | 触发 | 依赖 |
|---|---|---|---|
| fork-checks.yml | pull_request / push / dispatch | diff 选中 | manifest 全部 + attestation |
| fork-release-prepare.yml | workflow_dispatch | `ci_run_id`、`stage`、`inventory_json`、`continue_from` | 一次成功的完整 fork-checks |
| fork-cd-cloudflare.yml | workflow_dispatch / workflow_call | `delivery_run_id`、`stage`、`qualification_artifact`、`reset_*` | 一次成功的 prepare run |
| fork-cd-server.yml | workflow_dispatch / workflow_call | `delivery_run_id`、`stage`、`qualification_artifact` | 一次成功的 prepare run |
| fork-upstream-sync.yml | cron（周一 02:00）+ dispatch | 无 | upstream/main fetch |
| fork-runner-probe.yml | workflow_dispatch（手动） | 无 | self-hosted runner 能力报告 |

admission 链：`release_ci.py verify` → `release_ci.py resolve` → `release_ci.py ci` → `download_delivery.py` → `release_archive.py unpack_candidate` → `deploy-cloudflare.mjs` / `deploy_server.py`。**没有 manifest id 来验证 release lane 是否真的写过 attestation**：第 5 节列出本轮的修复方向。

## 4. 真实命令与脚本清单

| 用途 | 入口 | 备注 |
|---|---|---|
| 后端 runner | `bash backend/test.sh` | 完整 runner，本轮 1,154 个文件通过。 |
| 后端 fork 子集 | `BACKEND_UNIT_TEST_FILE_LIST=<list> bash backend/test.sh` | 由 `fork-backend-seams` / `fork-selfhost-startup` 使用。 |
| 上游 hygiene | `make preflight` | 上游 163 条本地 lane。 |
| fork 本地全套 | `scripts/fork/preflight` | 上游 + fork 双门禁。 |
| fork-only | `scripts/fork/preflight --fork-only` | 仅 fork manifest。 |
| self-host 契约 | `bash deploy/self-host/ci/product.sh` | 16 真实 HTTP 合同。 |
| cloudflare 契约 | `bash deploy/cloudflare/ci/product.sh` | 16 真实 HTTP 合同。 |
| web 构建 | `bun deploy/web/build.ts --target <t> --stage <s> --manifest <m> --output <o>` | 28 routes。 |
| web ci | `bash deploy/web/ci.sh` | 10 tests / 88 assertions。 |
| contracts core | `python3 contracts/deployment/core.py --metadata <m>` | 共享合同套件。 |
| 上游 touch | `python3 scripts/fork/check-upstream-touch.py --aggregate` | 必须 `upstream/main` 已 fetch。 |
| workflow lint | `python3 scripts/fork/workflow_lint.py` | actionlint + fork label 目录。 |
| overlay owner | `python3 scripts/fork/check-overlay-owners.py` | flutter/macOS/Electron 三方。 |
| profile 渲染 | `python3 scripts/profiles/render.py --target self_hosted --stage local --emit-json` | 默认 native；缺模型拒绝启动。 |
| profile 校验 | `python3 scripts/profiles/check_tables.py` | 四端一致。 |
| repo state 静态 | `python3 scripts/fork/check_repo_state.py` | GITHUB_ACTIONS=true 时再 live。 |
| repo state plan | `python3 scripts/fork/apply_repo_state.py --plan-only` | **不调网络**，CI 安全。 |
| repo state verify | `python3 scripts/fork/apply_repo_state.py --verify` | 需要 admin token，operator-only。 |
| fork release | `python3 scripts/fork/prepare_release.py --brand eddy --stage <beta\|production> …` | freeze；由 prepare workflow 调用。 |
| fork deploy CF | `node scripts/fork/deploy-cloudflare.mjs --delivery <dir>` | 由 fork-cd-cloudflare.yml 调用。 |
| fork deploy Server | `python3 scripts/fork/deploy_server.py <deploy\|qualify> --delivery <dir> …` | 由 fork-cd-server.yml 调用。 |

## 5. Stage 矩阵（`contracts/deployment/README.md` 承诺）

| target | stage | 入口 | 真实状态 | 本机 |
|---|---|---|---|---|
| self_hosted | `boot/public` | `contracts/deployment/core.py --metadata <m>` | ✅ | 可 |
| self_hosted | `frozen/local` | `bash deploy/self-host/ci/product.sh` | ⚠️ entry 不接受 `--stage` | 可 |
| cloudflare | `frozen/local` | `bash deploy/cloudflare/ci/product.sh` | ⚠️ entry 不接受 `--stage` | 可 |
| cloudflare | `frozen/private` | （未实现） | ❌ | 不可 |

**已知漂移**：

1. `deploy/cloudflare/ci/product.sh` 与 `deploy/self-host/ci/product.sh` 没有 `--stage` 入口，但 `deploy/cloudflare/contracts/local-target.mjs` 与 `deploy/self-host/ci/product.py` 已支持 stage / brand / self-test。`fork-staged-targets` 是薄守卫，任何增删都会立刻红。
2. `fork-cloudflare-product-core` 与 `fork-selfhost-product-core` 的 manifest id 名称与 stage 不一致（用了 `frozen/local` 默认值，但 id 没有 `_frozen_local` 后缀）。
3. `fork-cloudflare-routes` trigger 列里没有 `deploy/cloudflare/scripts/frozen-local.mjs`（由 staged-owners 兜底，但新增路径请同步登记）。

## 6. 已知失效 / 未来动作

| 缺口 | 影响 | 处置 |
|---|---|---|
| `fork-release-prepare.yml` / `fork-cd-cloudflare.yml` / `fork-cd-server.yml` 不在 manifest | 改完 workflow 不被 CI 检查；只能依赖 `fork-workflow-lint` | 由 `fork-workflow-lint` 与 `fork-ci-diff-base` 兜底；本身已是回归测试。 |
| `apply_repo_state.py --verify` 在 CI 不可达 | ruleset 与 environment 的一半只靠 operator | `fork-repo-state-apply` 把 planner 放进 CI；verify 仍是 operator-only。 |
| `desktop/macos/docs/desktop-updates.mdx` 超预算（4/1） | `fork-arch-doc-budget` / `brand-ui` 类检查会红 | 基线已红，本轮没动；合并时需手工调整。 |
| fork self-host 在 arm64 host 上 QEMU 模拟 | 慢 | x86 runner 已接，PR lane 仍走 `ubuntu-latest` 不污染可信路径。 |
| 真实模型运行时 | 离线需 1.5 TB 自由 + Docker + 已 fetch 的 BGE-M3/Qwen/SenseVoice/Kokoro | CI lane 用 `prepare-*.py` 自动拉；本地靠 `.local/selfhost-models/`。 |
| `fork-selfhost-product-core` 2026-09-04 已删的 `providers.py` / `test_provider_access.py` 文字痕迹 | manifest reason 字段过期 | 已在本轮清理。 |

## 7. 改 manifest / workflow 的清单

1. `validate_manifest` 要求每条 id 唯一、命令存在、`triggers` glob 命中现有路径、lanes 同时含 `local` + `ci`、`platforms ∈ {all,macos,linux,windows}`。改完先在本地：

   ```bash
   backend/.venv/bin/python -c "
   import sys; from pathlib import Path
   sys.path.insert(0, '.github/scripts')
   from run_checks import load_manifest, validate_manifest
   print(validate_manifest(load_manifest(Path('.github/checks-manifest.fork.yaml')), Path('.')))
   "
   ```

2. 改 workflow 后：

   ```bash
   go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12
   PATH="$HOME/go/bin:$PATH" python3 scripts/fork/workflow_lint.py
   ```

3. 改 release / CD workflow：单独跑 `test_release_prepare.py` / `test_release_ci.py` / `test_release_failure_summary.py` / `test_deploy_server.py` / `test_hosted_core.py` / `test_model_services.py` / `test_reset_cloudflare_stage.py` 七个。

4. 改 `apply_repo_state.py`：同步更新 `config/repo-state.fork.json` 与 `test_apply_repo_state.py`。

5. 改 `scripts/fork/preflight` 或 `scripts/fork/run_checks.py`：在 PR 描述里说明对 fork-checks.yml 行为的影响。

## 8. 不在 manifest 的 CD workflow 仍受 `fork-workflow-lint` + `fork-ci-diff-base` 覆盖

- `fork-cd-cloudflare.yml`、`fork-cd-server.yml`、`fork-release-prepare.yml`、`fork-upstream-sync.yml`、`fork-runner-probe.yml` 都用 `fork-*.yml` glob。
- 改它们的入口（`release_ci.py` / `release_failure_summary.py` / `download_delivery.py` / `deploy-cloudflare.mjs` / `deploy_server.py` / `reset_cloudflare_stage.py` / `prepare_release.py`）会被 `fork-ci-diff-base` 命中并跑 `test_*.py`。
- 改 `actions/fork-release-tools/action.yml`（toolchain）只会被 `fork-workflow-lint` 兜底，没有运行时断言；本机验证靠 `make setup-backend` + `oven-sh/setup-bun@v2`。
