# 05 · CI：上游工作流不动，fork 工作流走矩阵（品牌 × 部署目标）

> **2026-09-11 取代说明（先读这段）**：本文是 2026-09-04 的**规划稿**，其中 §4（`deploy/matrix.json`）、§5 的 `fork-build-matrix.yml` / `fork-contract-*.yml` / 标签触发的四条 `fork-deploy-*` 工作流、§8 的 C2–C5 **从未实现**，也不会实现。实际落地的是"冻结一次 + 两个目标各自资格化与发布"的形态：本文 §3 的 `fork-checks.yml` + `checks-manifest.fork.yaml` 那条线，加上 `fork-release-prepare.yml`、`fork-cd-cloudflare.yml`、`fork-cd-server.yml`、`fork-upstream-sync.yml`。
>
> 实际设计（准入、失败证据、品牌契约、仓库状态声明）以
> [`scripts/fork/RELEASE.md`](../../scripts/fork/RELEASE.md) 与
> [`scripts/fork/README.md`](../../scripts/fork/README.md) 为准；本文只保留规划语境。
> §1 的七条原则仍然有效，只是原则 4 的"矩阵是数据"由 `deploy/profiles/*.yaml` +
> `brand/*/manifest.yaml` 承担，而不是一个 `deploy/matrix.json`。
>
> **本轮（2026-09-11）的设计修正**记录在 `decisions.md` 的 D14–D19：上游零改动改为
> 状态审计且无法评估即失败；仓库状态（workflow 启停 / 必选检查 / 环境防护）由
> `config/repo-state.fork.json` 声明并由 `fork-repo-state` 断言；发布品牌成为仓库常量；
> CD 失败写脱敏根因；完整 lane 产出 manifest attestation 作为发布准入的一半。

> 依据（main 现状）：`.github/scripts/run_checks.py` 支持 `--manifest <path>`（`:453`），但 `pr_preflight.py:61` 写死单一清单路径、`validate_manifest` 要求每条检查同时声明 `local` 与 `ci` 通道；`.github/actions/detect-changes` 输出 24 个 `has_*` 标志；`runtime_image_contracts.yml:36-58` 是仓库里唯一"无 GCP 密钥时优雅降级"的工作流；5 个工作流会把提交写回仓库（每周冲突源）。

## 0. 实际实现的 lane 模型（2026-09-11）

| lane | 触发 | runner | 选择范围 | 可授权发布 | 并发组 |
|---|---|---|---|---|---|
| pr | `pull_request → main` | `ubuntu-latest` / `macos-26` | diff（目标分支为 base） | 否 | `fork-checks-<ref>-pull_request` |
| main-push | `push → main,codex/**,sync/**` | 自托管 Mac Studio | diff（`event.before` 为 base） | 否 | `fork-checks-<ref>-push` |
| release | `workflow_dispatch`（仅 main） | 自托管 Mac Studio | **全量 manifest** + 产出 attestation | **是** | `fork-checks-<ref>-workflow_dispatch` |
| freeze+qualify | `workflow_dispatch`（仅 main） | 自托管 Mac Studio | 复用 release lane 的 run（API 校验） | 否 | `fork-prepare-<ref>-eddy-<stage>` |
| cd | `workflow_dispatch` / `workflow_call`（仅 main） | 自托管 Mac Studio | 冻结件 | — | `fork-cd-<target>-eddy-<stage>` |

不变量与强制手段的对应关系：

| 不变量 | 强制手段 |
|---|---|
| 上游文件零改动（白名单除外） | `fork-upstream-touch --aggregate`（完整分歧审计；缺 `upstream/main` → exit 2） |
| 每条 fork 检查声明 `local`+`ci` | 上游 `run_checks.py validate_manifest` |
| fork workflow 语法与 runner label 合法 | `fork-workflow-lint`（glob `fork-*.yml`） |
| 只有跑完整份 manifest 的 run 可授权发布 | `fork-ci-attestation.json` + `release_ci.py` 的覆盖校验 |
| 发布源已并入 main / 冻结件字节一致 | `release_ci.py` 的 `compare` 与 archive digest 校验 |
| workflow 启停集合 = 声明集合 | `fork-repo-state`（live 模式，`actions: read`） |
| `main` 必选检查、环境防护 | `config/repo-state.fork.json` + `apply_repo_state.py --verify`（operator，管理员凭据） |
| 常红检查不得进入必选集 | policy 的 `quarantine`（必须带可解析 tracking） |
| 发布失败留下脱敏根因 | `journal.failure` + `release_failure_summary.py` |


## 1. 原则

1. **上游工作流一个字节不改**（改 = 每次同步冲突）。不需要的在 GitHub Actions 界面禁用；启停集合现在声明在 `config/repo-state.fork.json` 并由 `fork-repo-state` 断言，不再靠一次性脚本（§2 的脚本已作废）。
2. **fork 工作流一律新文件** `.github/workflows/fork-*.yml`；fork 检查一律进 `.github/checks-manifest.fork.yaml`，由 `run_checks.py --manifest` 显式执行，不改 `pr_preflight.py`（把"清单 include"作为可选 PR 回推上游，接受后再简化）。
3. **没有密钥也必须绿**：所有需要密钥的 job 先跑一个 gate step 输出 `has_secrets`，后续 step 以 `if: steps.gate.outputs.has_secrets == 'true'` 跳过，照抄 `runtime_image_contracts.yml` 的做法（`secrets` 上下文不能直接用于 job 级 `if`）。
4. **矩阵是数据**（原规划 `deploy/matrix.json` 未实现）：品牌 × 目标的实际数据源是 `deploy/profiles/*.yaml` + `brand/*/manifest.yaml`，由 `fork-profile-tables` 与 `fork-profile-boundary-tests` 断言一致；发布品牌另由 `config/repo-state.fork.json` 的 `deploy_brand` 声明。
5. **标签命名空间隔离**：fork 发布标签统一前缀 `<brand>/…`（如 `mw/selfhost/v1.4.0`、`mw/cloudflare/v1.4.0`、`mw/macos/v0.13.0`、`mw/fw/cv1/v3.1.0`），并设置 `git config remote.upstream.tagOpt --no-tags`，避免上游数千个 `v*-macos`/`Omi_CV1_v*` 标签污染 fork 与触发误配。

## 2. 上游工作流处置（一次性，`gh workflow disable`）

> **2026-09-11 取代说明**：下面这段一次性脚本已被 `config/repo-state.fork.json` +
> `scripts/fork/check_repo_state.py` + `scripts/fork/apply_repo_state.py` 取代。
> 保留它是因为它记录了当初的判断；**不要再手工执行**。要改启用集合，改 policy 文件，
> 然后跑 `python3 scripts/fork/apply_repo_state.py --dry-run`。理由：手写脚本没有记录
> "哪些该开"，于是每次上游同步带进新 workflow（默认启用）时都要靠人回忆；2026-09-10
> 的同步就是这样手工关掉四个 `gcp_*` 文件的。现在 `fork-repo-state` 会在每个 lane 断言
> 注册集合与启用状态，`--verify` 覆盖 CI 拿不到管理员凭据的那一半。

```bash
# 在 fork 仓库执行一次；被禁用的工作流文件保留在树里，同步时零冲突
for wf in gcp_admin gcp_app gcp_backend gcp_backend_auto_dev gcp_backend_listen_helm gcp_backend_pusher \
          gcp_backend_pusher_auto_deploy gcp_diarizer gcp_firestore_indexes gcp_frontend gcp_llm_gateway \
          gcp_memory_maintenance_job gcp_memory_maintenance_job_auto_dev gcp_models gcp_nllb_translation \
          gcp_notifications_job gcp_parakeet gcp_personas gcp_plugins \
          desktop_auto_release desktop_backend_auto_dev desktop_backend_prod desktop_backend_recover_prod \
          desktop_beta_admission_control desktop_breakglass_credential_preflight desktop_breakglass_rollout_beta \
          desktop_codemagic_failure_recovery desktop_promote_beta desktop_promote_prod desktop_publish_preview \
          desktop_recover_beta desktop_release_doctor desktop_rollback_beta desktop_windows_release \
          mobile_internal_build publish_omi_cli firmware_release deploy_docs sync-docs sync_ledger_fence_cutover \
          guardrail-baseline-pulse repo-hygiene main entellegence_issues entelligence-pr-reviewer \
          onboarding_figma_sync opentofu-development-wif-pilot opentofu-development-wif-pilot-validate \
          opentofu-foundation-validate parakeet_gpu_tests task-recommendation-live-eval; do
  gh workflow disable "$wf.yml" 2>/dev/null || echo "skip $wf"
done
```

**保留启用**（无密钥即可绿、且对 fork 有价值）：`repo-checks`、`backend-checks`、`backend-unit-tests`、`backend-hermetic-e2e`、`mobile-app-checks`、`desktop-checks`、`desktop-swift-ci`、`desktop-windows-ci`、`web-checks`、`openapi-contract`、`desktop-backend-contracts`、`public-build-config-preflight`、`runtime_image_contracts`（已自带降级）、`python-cli-ci`、`sdk-rust`、`windows-preflight-portability`、`release-eligibility`（作为"主线可发布性"证明沿用）。

被禁用的写回机器人（`guardrail-baseline-pulse` 直推 `.github/guardrail-pulse-history.jsonl`；`sync-docs` 重生成 SDK README；`repo-hygiene` 开 failure-class 退休 PR；两个 desktop release 工作流推 changelog）是今天 13 个同步冲突中 1 个的直接来源，也是未来冲突的最大潜在来源。

## 3. fork 检查清单与本地入口

**2026-09-08 fork CI 调整**：当前实现见
[fork 脚本说明](../../scripts/fork/README.md)。`summersmile1984/omi` 的
`main`、`codex/**`、`sync/**` push 和手动运行选择 Mac Studio runner；
面向 `main` 的 PR 仍使用托管 runner。Server OS 运行 Linux Docker 契约，
Cloudflare 运行本地 Wrangler 契约，原生 macOS 检查单独调度。
新分支首次 push / 手动运行检查相对 main 的完整分支差异；上游零改动规则
使用实际已合入的上游祖先，不受远端 tip 前移影响。失效的 storage/queue
测试引用已迁到 `backend/fork/tests/test_storage_queue_adapters.py`。
`scripts/fork/preflight --fork-only` 对应 Fork Checks；默认命令仍保留上游加
fork 的完整门禁。下文保留原有规划语境，不代表部署 workflow 已经实现。

2026-09-04 的实际工作流以 `.github/workflows/fork-checks.yml` 和
`.github/checks-manifest.fork.yaml` 为准；下表仍包含规划项。PR 使用目标分支的
已获取提交作 diff base，push 使用事件的 `before`；手动运行没有 `before`，
使用当前提交的父提交，因此检查当前提交的变更。无法解析提交时立即失败。
`fork-ci-diff-base` 在真实临时 Git 仓库中执行工作流原始 shell，覆盖上述事件，
避免手动入口再次传入空 ref。

原生 macOS 的现有 fork CI 入口使用共用 manifest selector 按 diff 选出
macOS-only 检查；Linux gate 通过后，有原生检查才启动标准 `macos-26` runner。
固定 Xcode 26.6 和 Python 3.12，执行 native identity 测试及完整 staged debug
编译。两项都同时注册 local/ci，Linux 的平台跳过不计为原生验证通过。
SwiftPM 只缓存下载依赖，不复用带绝对路径的 `.build`，也不签名、发布或启动应用。

`.github/checks-manifest.fork.yaml`（与上游同 schema：`id/command/triggers/lanes/reason`，每条都声明 `["local","ci"]`）：

| id | command | triggers | 目的 |
|---|---|---|---|
| `fork-brand-apply-clean` | `python3 scripts/brand/apply.py --brand ${BRAND:-omi-upstream} --check-clean` | `brand/**`, `scripts/brand/**`, 各注入点文件 | 生成物与清单一致 |
| `fork-brand-leak-scan` | `python3 scripts/brand/check.py --brand ${BRAND:-omi-upstream}` | `app/lib/**`, `desktop/**`, `backend/**`, `web/**`, `docs/**`, `omi/firmware/**` | 用户可见面零上游品牌词（`omi-upstream` 品牌下为基线 ratchet） |
| `fork-upstream-touch` | `python3 scripts/fork/check-upstream-touch.py --base upstream/main --allowlist dev/unified-main/upstream-touch-allowlist.yaml` | `all` | **默认零个上游文件被修改**；例外只能来自 T1 白名单（逐条限行数、附上游 PR 链接）；命中 T2 类别（上游测试/锁文件/生成文件/机器人文件/CI/格式化）直接失败。见 `00-upstream-touch-policy.md` |
| `fork-profile-consistency` | `python3 scripts/profiles/check_tables.py` | `deploy/profiles/**`, 各端生成的 profile 表 | Flutter/Swift/TS/Python 四份 profile 表由同一源生成且一致（见 `02-deployment-profile.md`） |
| `fork-selfhost-compose-valid` | `docker compose -f deploy/self-host/compose.production.yml config -q` | `deploy/self-host/**` | compose 可解析、无缺失变量 |
| `fork-cloudflare-routes` | `bash deploy/cloudflare/ci/routes.sh` | `deploy/cloudflare/**` 与上游路由/导出器依赖 | 已在两 lane 注册：真实注册路由漂移、manifest、TS/全Vitest（含发布事务/HTTP/进程/冻结工件回归）、Core tests；同源码双 Web + 16次 Worker工件 dry-run 在 `release.mjs prepare` 发布资格入口执行，当前远端资格仍 pending。 |
| `fork-agents-doc-refs` | `python3 .github/scripts/check_agent_doc_references.py --extra AGENTS.fork.md backend/AGENTS.fork.md app/AGENTS.fork.md desktop/macos/AGENTS.fork.md` | `**/AGENTS*.md` | fork 规则文档引用可解析（上游脚本按文件名精确匹配 `AGENTS.md`，`AGENTS.fork.md` 默认不可见，需显式传入） |
| `fork-matrix-valid` | `python3 scripts/fork/check-matrix.py deploy/matrix.json` | `deploy/matrix.json`, `brand/*/manifest.yaml` | 矩阵引用的品牌/目标/组件存在 |

本地入口 `scripts/fork/preflight`：

```bash
#!/usr/bin/env bash
# 与 make preflight 并列：先跑上游清单，再跑 fork 清单
set -euo pipefail
root=$(git rev-parse --show-toplevel)
python3 "$root/.github/scripts/pr_preflight.py" --lane local --base origin/main
python3 "$root/.github/scripts/run_checks.py" --manifest "$root/.github/checks-manifest.fork.yaml" --lane local --base origin/main
```

`Makefile.fork`（不改上游 `Makefile`）：`preflight-fork`、`brand-apply BRAND=…`、`brand-check BRAND=…`、`selfhost-up`、`cf-dev`、`sync-upstream`。

## 4. 矩阵定义 `deploy/matrix.json`（未实现，被取代：实际由 `deploy/profiles/*.yaml` + `brand/*/manifest.yaml` 承担）

```json
{
  "schema_version": 1,
  "brands": ["omi-upstream", "memweft"],
  "targets": {
    "selfhost":   { "backend": "backend", "auth": "auth-server", "contract_runner": "deploy/self-host/ci/contract.sh" },
    "cloudflare": { "backend": "deploy/cloudflare", "auth": "deploy/cloudflare/workers/auth", "contract_runner": "deploy/cloudflare/ci/contract.sh" }
  },
  "clients": {
    "flutter": { "build": "app/scripts/ci-build.sh", "needs_secrets": ["APPLE_SIGNING", "ANDROID_KEYSTORE"] },
    "macos":   { "build": "desktop/macos/scripts/ci-build.sh", "needs_secrets": ["APPLE_SIGNING", "SPARKLE_PRIVATE_KEY"] },
    "windows": { "build": "desktop/windows/scripts/ci-build.sh", "needs_secrets": ["WINDOWS_CODESIGN"] },
    "web":     { "build": "web/app/scripts/ci-build.sh", "needs_secrets": [] }
  },
  "exclude": [ { "brand": "omi-upstream", "target": "cloudflare" } ]
}
```

规则：`omi-upstream` 品牌只用于回归（证明 fork 未破坏上游等价性），不发布。每个 `brand × target` 交叉点必须能：① 生成品牌；② 以该目标的 profile 构建全部客户端；③ 对该目标后端跑契约套件。

## 5. fork 工作流一览（规划稿，未实现；实际见 `scripts/fork/RELEASE.md` 与 §0）

| 文件 | 触发 | 作用 | 密钥 |
|---|---|---|---|
| `fork-checks.yml` | PR、push main | 跑 `checks-manifest.fork.yaml`（ci 通道）；对 `matrix.brands` 每个品牌跑 `apply --check-clean` + `check.py` | 无 |
| `fork-contract-selfhost.yml` | PR 触及 `backend/**`、`deploy/self-host/**`、`auth-server/**`、`contracts/**` | `docker compose -f deploy/self-host/compose.ci.yml up`（postgres、redis、minio、auth-server、backend，STT/LLM 用 stub provider）→ `contracts/` 一致性套件 + OpenAPI `--check` + 登录/记忆/对话烟测 | 无 |
| `fork-contract-cloudflare.yml` | PR 触及 `deploy/cloudflare/**`、`contracts/**`、`web/app/**` | `wrangler dev`（本地 D1 迁移 + miniflare）起 edge/auth/api-core/api-ai → 同一套契约套件 | 无（`CLOUDFLARE_API_TOKEN` 缺失时跳过需要远端绑定的用例） |
| `fork-build-matrix.yml` | PR、push main | `strategy.matrix` 来自 `deploy/matrix.json`：品牌 × 客户端；无签名密钥时只做 analyze/test/未签名构建，有密钥时产出可安装件 | gate |
| `fork-deploy-selfhost.yml` | 标签 `<brand>/selfhost/v*`、dispatch | 构建镜像（上游 `backend/Dockerfile` + `requirements-fork.txt` 叠加层、`auth-server/Dockerfile`）→ 推镜像 → `deploy/self-host/operations.sh deploy`（SSH 或 kubeconfig）→ `cutover-live-smoke.py` | `FORK_REGISTRY_*`、`FORK_SELFHOST_SSH_KEY`/`KUBECONFIG` |
| `fork-deploy-cloudflare.yml` | 标签 `<brand>/cloudflare/v*`、dispatch | `wrangler deploy` 依赖顺序（rate-limit → auth → jobs → realtime → api-* → edge → web），D1 迁移 apply + verify，`smoke:production` | `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`、各 Worker secrets |
| `fork-release-macos.yml` | 标签 `<brand>/macos/v*` | 复用上游 `codemagic.yaml` 的 `omi-desktop-swift-release` 逻辑但用 fork 的 `codemagic.brand.yaml` 变量段（或迁到 GitHub macOS runner）；产出 `<brand>.zip`/`.dmg` + appcast 条目 | Apple 签名、Sparkle 私钥 |
| `fork-release-firmware.yml` | 标签 `<brand>/fw/cv1/v*` | `west build` + `brand.conf` + secret 写入的 MCUboot 私钥 → 资产 `<prefix>_OTA_v*.zip` | `MCUBOOT_SIGNING_KEY_PEM` |
| `fork-upstream-sync.yml` | 每周一 cron、dispatch | 见 `06-upstream-sync.md` §6 | 无 |

Gate step 模板（放 `.github/actions/fork-secret-gate/action.yml`）：

```yaml
runs:
  using: composite
  steps:
    - id: gate
      shell: bash
      run: |
        missing=()
        for k in $REQUIRED; do [ -n "${!k:-}" ] || missing+=("$k"); done
        if [ ${#missing[@]} -eq 0 ]; then echo "has_secrets=true" >> "$GITHUB_OUTPUT"; else echo "has_secrets=false" >> "$GITHUB_OUTPUT"; echo "::notice::skipping signed/deploy steps, missing: ${missing[*]}"; fi
```

调用方把所需 secrets 映射为 env 传入（`env: { APPLE_SIGNING: ${{ secrets.FORK_APPLE_SIGNING }} }`），后续 step 用 `if: steps.gate.outputs.has_secrets == 'true'`。

## 6. 密钥与变量命名

- 仓库变量（非密）：`FORK_DEFAULT_BRAND`、`FORK_TARGETS`、`FORK_REGISTRY`。
- 密钥统一前缀 `FORK_`，按维度分组：目标级（`FORK_CLOUDFLARE_API_TOKEN`、`FORK_CLOUDFLARE_ACCOUNT_ID`、`FORK_SELFHOST_SSH_KEY`、`FORK_KUBECONFIG`、`FORK_REGISTRY_TOKEN`）；品牌级（`FORK_<BRAND>_APPLE_CERT_P12`、`FORK_<BRAND>_SPARKLE_PRIVATE_KEY`、`FORK_<BRAND>_MCUBOOT_KEY_PEM`、`FORK_<BRAND>_WINDOWS_CODESIGN`、`FORK_<BRAND>_POSTHOG_KEY`）。
- `config/deployment-setting-classification.json` 是上游文件：fork 新增变量写入 `config/deployment-setting-classification.fork.json`，`fork-checks.yml` 用同一脚本加 `--extra` 参数校验（脚本支持与否见 PR C2；不支持则 fork 复制一份脚本到 `scripts/fork/`）。

## 7. 与上游 CI 的关系（避免双跑与误报）

- 上游 `repo-checks.yml` 的 hygiene 通道继续跑上游清单；fork 清单由 `fork-checks.yml` 跑。两者互不包含，PR 必须两者都绿。
- `mobile-app-checks.yml` 会用空 `.dev.env` 与 `firebase_options_local.dart` 覆盖 dev/prod 配置（`:130-141`），对 fork 的 profile 表无副作用，但 **fork 的 Flutter 构建矩阵必须用生成后的品牌配置**（`fork-build-matrix.yml` 先跑 `apply.py`）。
- `openapi-contract.yml` 校验的三份 OpenAPI 由 `export_openapi.py` 生成，B4 之后其标题/contact 读 `brand.py`：在 `omi-upstream` 品牌下输出必须与上游字节一致，否则该上游工作流会红——这是"上游等价性"的天然守卫。
- **两条测试通道**：上游组件测试（`backend/test.sh`、`app/test.sh`、Swift/Windows 测试、`web-checks`）在"上游模式"运行，不设任何 shim/profile 环境变量、不启用 `pubspec_overrides`/`vite.fork.config`，证明 fork 未改变上游行为；fork 测试目录在 `self_hosted`/`cloudflare` 模式运行。上游测试文件永不修改。
- `release-eligibility.yml` 沿用：它证明 `main` 上每个提交通过了全部 ci 通道检查；fork 的部署工作流以它为前置（`workflow_run` 或读取其 check-run 状态）。

### 7.1 已知红：`desktop-beta-admission-firestore-contention`（上游缺陷，M1 首次撞上）

**任何往仓库里新增 `package.json` 的 fork PR 都会让 `Desktop Swift Static & Test Contracts` 变红**，原因与该 PR 的内容无关：

1. 上游 `.github/scripts/run_checks.py` 的 `_matches()` 用 `PurePath(path).match(pattern)`，而 `PurePath` 对不含斜杠的模式是**从右往左匹配**。清单里为仓库根写的触发器 `package.json`，因此会被 `auth-server/package.json`（M1 新增）命中。
2. 被选中之后该检查必然失败：`run.sh` 只在没有 `backend/.venv` 时才走 `uv run --no-project --with google-cloud-firestore --with prometheus-client`，而 `firestore_contention_test.py` 的导入链是 `database.staged_tasks` → `database.action_items` → `utils.observability.fallback` → `utils.metrics` → **`fastapi`**，不在那个依赖集里。开发者本地有 `.venv`，走的是另一条分支，所以这个缺陷至今没被发现。

已在 `origin/main` 的干净工作树上用同一条 `uv run` 命令复现，与 fork 改动无关。加上 `--with "fastapi==0.121.0"` 后脚本跑完全部导入。

**fork 不修它**：`backend/**` 是 T2 禁改区（`upstream-touch-allowlist.yaml` 的 `forbidden_patterns`），`.github/checks-manifest.yaml` 与 `.github/workflows/**` 同样禁改，所以两个缺陷 fork 侧都没有合法修法。两条修复已进 `upstream-prs.md`（#13、#14）。在上游接受之前，带 `package.json` 的 fork PR 以此条为准判定该检查为**已知红**，不得为了变绿去改上游文件，也不得因此放宽 T2。

### 7.2 已知红：`backend-unit-tests`（上游基线滞后，待下次同步）

`origin/main` 上这一对文件来自不同的上游时间点：

- `backend/utils/conversations/projection_payload.py` 在**模块作用域**执行 `from models.client_processing import PROJECTION_FAMILY_FIELDS`；
- `backend/tests/unit/test_batch_upload_storage.py` 与 `test_merge_validation.py` 用 `stub_modules` 安装一个裸的 `models` 包，**没有** `models.client_processing` 这一项。

裸 `ModuleType` 没有 `__path__`，所以子模块导入直接 `ModuleNotFoundError`。两个文件在 `upstream/main` 上都已修好（stub 列表补了 `models.client_processing`，导入移进函数体并附了原因注释），因此这不是 fork 缺陷，也不是上游缺陷，而是**同步滞后**：下一次 `fork-upstream-sync` 合并 `upstream/main` 后自动消失。

**处置**：`backend/**` 是 T2 禁改区，`**/tests/**` 还是 `forbidden_patterns`，fork 侧没有合法修法，也不应为了让检查变绿去改上游测试或加一个 fork 的 `conftest.py` 掩盖基线不一致。登记在 `config/repo-state.fork.json` 的 `quarantine` 里：保持启用（信号还在），但不进入 `main` 的必选检查，并在同步后复查。`fork-repo-state` 会拒绝一个没有 tracking 指针、或仍然参与必选检查的隔离条目。

### 7.3 已知红：`Typecheck · Lint · Test`（fork 自有 lint 债，44 项）

`desktop-windows-ci.yml` 的 lint 步骤自 unified-delivery 分支落地以来每次都失败。run 34567708664 报 **44 errors / 846 warnings**，按文件归属全部落在 **fork 自有的 `desktop/windows/fork/**`**，没有一项在上游文件里：

| 规则 | 数量 | 主要位置 |
|---|---|---|
| `@typescript-eslint/explicit-function-return-type` | 19 | `fork/source-stage.mjs`（11）、`fork/assets.mjs`、`fork/brand-stage.mjs`、`fork/tests/staged/*` |
| `@typescript-eslint/no-explicit-any` | 16 | `fork/tests/**` |
| `@typescript-eslint/no-empty-function` | 7 | `fork/assets.mjs` 等 |
| 其他 | 2 | `no-useless-escape`、`no-unused-vars` |

846 条 warning 全部是 `prettier/prettier`，不阻塞。

**这不是 T2 冲突**：上游文件零改动，全是 fork 自己的代码，所以**有合法修法**，只是修法需要真实桌面工具链重跑 staged 测试，属于独立改动。本分支只删掉了 `fork/source-stage.mjs` 里未使用的 `node:fs` 导入（45 → 44），并把该检查从必选移到隔离。

**处置**：登记在 `config/repo-state.fork.json` 的 `quarantine`（tracking 指向本节）。修完 44 项后应把它提升回 `required_jobs`，那是一次独立的 PR：本地 `pnpm install --frozen-lockfile && pnpm lint && pnpm test` 通过即可作为验收。

## 8. 落地 PR（对应 `07-pr-plan.md` 的 C 系列；C0/C1/C6 已落地，C2–C5 未实现且已被取代）

| PR | 内容 | 验收 |
|---|---|---|
| C0 | `gh workflow disable` 脚本执行记录进 `dev/unified-main/sync-log.md`；`remote.upstream.tagOpt --no-tags` 写进 `Makefile.fork setup-fork` | Actions 页面只剩保留列表 + fork 工作流 |
| C1 | `checks-manifest.fork.yaml` + `scripts/fork/preflight` + `fork-checks.yml` + `Makefile.fork` | PR 上两条 hygiene 都绿；本地 `scripts/fork/preflight` 与 CI 结果一致 |
| C2 | `deploy/matrix.json` + `fork-build-matrix.yml` + secret gate action | 无密钥的 fork PR 全绿（只做未签名构建）；有密钥时产出可安装件 |
| C3 | `fork-contract-selfhost.yml` + `deploy/self-host/compose.ci.yml` + `deploy/self-host/ci/contract.sh` | 契约套件对自托管栈通过 |
| C4 | `fork-contract-cloudflare.yml` + `deploy/cloudflare/ci/contract.sh` | 同一契约套件对 `wrangler dev` 通过 |
| C5 | `fork-deploy-selfhost.yml`、`fork-deploy-cloudflare.yml`、`fork-release-macos.yml`、`fork-release-firmware.yml` | 各自在 staging 完成一次真实发布并留下证据文件 |
| C6 | `fork-upstream-sync.yml` + PR 模板 | 第一次自动同步 PR 生成并合并 |
| C7（可选，回推上游） | `pr_preflight.py`/`run_checks.py` 支持 `--manifest` 多文件或 `include:`；`check_agent_doc_references.py --extra`；`check_deployment_secret_boundary.py --extra` | 上游接受后删除 fork 侧的绕行 |
