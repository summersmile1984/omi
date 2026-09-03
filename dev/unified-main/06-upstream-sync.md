# 06 · 上游同步 Runbook（每周一次，冲突可度量、可下降）

> 适用：单主线拓扑落地后的常态运维。目标：`upstream/main → main` 每周一次，人只解真实冲突，冲突文件数从今天的 13 降到 ≤5 并保持。
> 今日基线（2026-09-02，`git merge-tree --write-tree main upstream/main`，只计真实冲突）：**13 个文件**；其中 6 个是 fork 自己的格式化提交造成的，3 个是 fork 的 shim 注入点，2 个是机器人/忽略文件，2 个是文档与测试。

## 1. 今天的 13 个冲突文件与永久处置

| 文件 | 冲突原因 | 永久处置（做一次，以后不再冲突） |
|---|---|---|
| `web/admin/app/api/omi/stats/{k-factor/posthog,profitability,viral-metrics}/route.ts`、`web/admin/grafana/build_dashboards.py`、`web/admin/grafana/dashboards/omi-tv.json`、`web/admin/lib/__tests__/platform-scope-routes.test.ts` | fork 提交 `c27893743e`/`a8d062cf45`/`93fae0b4eb` "style(admin): format …" 用不同版本的 prettier 重排上游文件 | **回退这三个提交的内容**（恢复为上游版本）；钉住格式化工具版本与上游 `.pre-commit-config.yaml` 一致；pre-commit 钩子只对 fork 自有路径格式化（见 §4） |
| `.github/guardrail-pulse-history.jsonl` | fork 与上游的 `guardrail-baseline-pulse.yml` 机器人各自每周追加同一文件 | 在 fork 的 GitHub Actions 界面**禁用** `guardrail-baseline-pulse.yml`；同步时该文件一律取上游版本（`git checkout --theirs`） |
| `.gitignore` | fork 追加了本地代理工具目录（`.codex/.loopx/...`，提交 `19e82722f4`） | 移到 `.git/info/exclude`（不入库）或 `dev/.gitignore`；根 `.gitignore` 恢复上游版本 |
| `backend/AGENTS.md` | fork 在上游文件里加了 shim 说明 | **取上游、不加指针**；正文移入 `backend/AGENTS.fork.md`（指针方案 2026-09-03 实测失败，预算无余量，见 §7 与 `00-upstream-touch-policy.md` §4 第 9 条） |
| `backend/config/stt_provider_policy.py`、`backend/utils/stt/streaming.py` | fork 加了 MiMo/MOSS/SenseVoice provider（`cc80aefad5`、`5c1dcd346f`、`ba3adaf967`） | provider 实现迁到 `backend/fork/stt/`，由 `backend/fork/main.py` 的补丁注册表在导入时注入到上游的 provider 表；**上游文件恢复原样（零改动）**；同时向上游提"provider 注册表"PR |
| `backend/utils/cloud_tasks.py` | fork 插入 `QUEUE_BACKEND=redis` 分发（`83e627b428`） | 同上：`backend/fork/patches/queue.py` 在导入时替换 `utils.cloud_tasks` 的派发函数，实现在 `backend/fork/cloud_tasks_redis.py`；上游文件零改动 |
| `backend/tests/unit/test_conversation_notes_v2.py` | fork 改了上游测试以适配 shim | 不改上游测试；shim 差异用 fork 自有测试文件覆盖（`backend/tests/unit/fork/`） |

预期：处置完毕后 `backend/**` 上游文件改动为 0，真实冲突降到 **0**（此后冲突只可能来自 T1 白名单那十来行，见 `00-upstream-touch-policy.md` §4）。

## 2. 每周流程（约 30~60 分钟）

```bash
# 0. 前置：干净工作区，在独立 worktree 里操作
git fetch upstream --prune
git worktree add ../wt-sync -b sync/upstream-$(date +%F) main
cd ../wt-sync

# 1. 先看代价，再动手（真实冲突数 = 以 CONFLICT 开头的行数）
git merge-tree --write-tree main upstream/main | grep -c '^CONFLICT'
git merge-tree --write-tree main upstream/main | grep '^CONFLICT' | sed -E 's/^CONFLICT \([^)]*\): Merge conflict in //'

# 2. 合并（merge，不 rebase；保留历史，不 force push）
git merge --no-ff upstream/main

# 3. 按 §3 规则解冲突；机器人文件与忽略文件直接取上游
git checkout --theirs .github/guardrail-pulse-history.jsonl && git add .github/guardrail-pulse-history.jsonl

# 4. 重跑品牌生成物与守卫（白牌层落地后）
python3 scripts/brand/apply.py --brand "$BRAND" && python3 scripts/brand/check.py

# 5. 本地契约门禁（与 CI 同一清单）
make preflight

# 6. 提交合并、开 PR、CI 绿后 merge（regular merge，不 squash），回到 main 拉取
git commit --no-edit
git push -u origin HEAD
gh pr create --title "sync: upstream/main $(date +%F)" --body-file dev/unified-main/templates/sync-pr-body.md
```

**PR 描述模板**必须记录：上游区间（`old..new` SHA）、真实冲突文件数、每个冲突文件的处置（ours/theirs/手工）、`check.py` 结果、`make preflight` 结果、两目标（self-host compose、Cloudflare `wrangler dev`）契约套件结果。

## 3. 冲突处置规则（按文件类别，机械执行）

| 类别 | 例子 | 规则 |
|---|---|---|
| 机器人生成 | `guardrail-pulse-history.jsonl`、changelog 自动更新、`*.lock`/`pylock*.toml` | **取上游**（theirs）。fork 依赖在 `requirements-fork.txt`，不碰上游锁文件 |
| 上游文件里的 fork 注入点 | `database/_client.py` 的 shim 开关、`endpoints.py` 的 `AUTH_PROVIDER` 分支、`stt_provider_policy.py` 的注册钩子 | 手工：保留上游新逻辑 + 保留 fork 那一行钩子；解完后**必须**运行对应 shim 单测 |
| 客户端接缝 | `auth_service.dart`、`auth_provider.dart`、`api.ts`、`DesktopBackendEnvironment.swift` | 手工：上游改动优先落地，fork 的 profile 分支重新套上；跑 profile 契约测试 |
| 品牌注入点 | `flavorizr.yaml`、`Info.plist` 模板、`brand.py` 调用处 | 取上游后重跑 `apply.py`，用 `check.py` 找新泄漏 |
| fork 自有路径 | `deploy/**`、`brand/**`、`backend/firestore_pg/**`、`auth-server/**`、`dev/**`、`*.fork.md` | 上游不会触碰，理论上无冲突；若冲突说明路径命名撞车，改 fork 路径 |
| 文档/AGENTS | `AGENTS.md`、`backend/AGENTS.md`、`PRODUCT.md` | **一律取上游**（`git checkout --theirs`）；fork 内容只存在于 `*.fork.md`，上游文件不加任何指针 |

## 4. "不修改上游文件"清单（fork 纪律，进 `AGENTS.fork.md` 并由检查脚本守卫）

1. **锁文件与依赖清单**：`backend/pylock*.toml`、`backend/requirements.txt`、`backend/pusher/requirements.txt`、`app/pubspec.lock`、`web/*/package-lock.json` → fork 依赖走 `backend/requirements-fork.txt`（`deploy/self-host/Dockerfile` 在上游镜像层之上 `pip install -r`）、`app/pubspec_overrides.yaml`（Dart 官方机制）、Web 用独立 `deploy/*/package.json`。
2. **机器人写入文件**：`.github/guardrail-pulse-history.jsonl`、`desktop/macos/CHANGELOG.json` 自动更新、`community-plugin-stats.json` → fork 禁用相应工作流，同步取上游。
3. **AGENTS/PRODUCT/规则文档**：上游文件**零改动、不加指针**（预算无余量）；正文进 `AGENTS.fork.md`、`backend/AGENTS.fork.md`、`app/AGENTS.fork.md`、`desktop/macos/AGENTS.fork.md`。`check_agents_md_lean.py` 按精确文件名 `AGENTS.md` 发现，fork 文件对它不可见、不受预算限制。
4. **检查清单**：`.github/checks-manifest.yaml` 不改；fork 检查写在 `.github/checks-manifest.fork.yaml`，运行器改为加载两个文件（一次性小改，可回推上游；见 `05-ci-matrix.md`）。
5. **CI 工作流**：上游 `gcp_*.yml`、`desktop_*release*.yml`、`mobile_internal_build.yml`、`publish_omi_cli.yml` **不删不改**，在 GitHub Actions 界面禁用；fork 工作流用新文件名 `fork-*.yml`。
6. **格式化**：`.pre-commit-config.yaml` 与上游一致；fork 钩子只对 `deploy/ brand/ backend/firestore_pg/ auth-server/ dev/ backend/utils/*_fork*` 等 fork 路径格式化。任何"style: format upstream files"提交禁止合入 `main`（检查脚本：若一次提交只改变空白/格式且触及非 fork 路径 → 失败）。
7. **上游测试**：不修改上游测试断言；fork 行为差异写 fork 自有测试（`backend/tests/unit/fork/`、`app/test/fork/`、`desktop/macos/Desktop/Tests/Fork*`），并保证被各组件 runner 发现（`backend-test-discovery` 清单检查会强制）。
8. **上游文件零改动为默认**：fork 行为通过新文件、包/模块别名、入口封装、导入时补丁、构建期生成文件与环境变量实现（技术目录见 `00-upstream-touch-policy.md` §3）。确实无法做到的（Swift 常量、Next 根配置、C 字面量）进 T1 白名单：单点、≤3 行、附上游 PR 链接，白名单只减不增。

守卫脚本 `scripts/fork/check-upstream-touch.py --allowlist dev/unified-main/upstream-touch-allowlist.yaml`（进 `checks-manifest.fork.yaml`，PR 触发）：对 PR diff 中属于"上游文件"（存在于 `upstream/main` 树）的每个文件——不在白名单 → 失败；在白名单但超过其行数上限 → 失败；命中清单 1/2/7 的 T2 类别 → 失败；并输出对应的 T0 做法提示。

## 5. 度量与告警

- 每次同步 PR 自动评论：真实冲突文件数、各文件处置、上游区间提交数。
- 趋势记录在 `dev/unified-main/sync-log.md`（日期、上游 SHA、冲突数、耗时、备注）。
- 阈值：冲突 > 5 → 本周内开一个"减注入点"任务；> 15 → 停止功能合入，先修拓扑。
- 每季度：统计"fork 改动的上游文件数"（`git log --no-merges --name-only upstream/main..main` ∩ 上游树），目标单调下降；每回推上游一个可配置化 PR 都应让这个数字减少。

## 6. 定时自动化（`fork-upstream-sync.yml`，草案）

```yaml
name: fork-upstream-sync
on:
  schedule: [{ cron: "0 2 * * 1" }]   # 每周一 02:00 UTC
  workflow_dispatch:
jobs:
  open-sync-pr:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - run: |
          git remote add upstream https://github.com/BasedHardware/omi.git
          git fetch upstream main
          n=$(git merge-tree --write-tree origin/main upstream/main | grep -c '^CONFLICT' || true)
          echo "conflicts=$n" >> "$GITHUB_OUTPUT"
        id: probe
      - run: |
          git switch -c sync/upstream-$(date +%F)
          git merge --no-ff upstream/main || true       # 冲突留给人解；PR 里标注
          git push -u origin HEAD
          gh pr create --title "sync: upstream/main $(date +%F) (conflicts: ${{ steps.probe.outputs.conflicts }})" \
                       --body "$(cat dev/unified-main/templates/sync-pr-body.md)"
        env: { GH_TOKEN: "${{ secrets.GITHUB_TOKEN }}" }
```

冲突为 0 时该 PR 只需 CI 绿 + 一人批准即可合并；冲突 > 0 时由值周人接手分支解冲突。


## 7. 同步 PR 的固定样板（2026-09-03 首次实战确认）

一次 1500 提交量级的上游同步，必然触发下面 5 项与"改动规模"挂钩的检查。它们与代码质量无关，是同步 PR 的样板工作，PR 正文模板里应预留位置：

| 检查 | 为什么会触发 | 处置 |
|---|---|---|
| `product-invariants` | diff 覆盖 3000+ 文件，几乎命中全部不变量 path glob | 在正文列出全部 ID（本次 17 个，含 `INV-AGENT-*`），并说明"守卫代码是上游的，本 PR 不改其行为" |
| `failure-class-protocol` | 区间里有大量上游 `fix:` 提交 | `Failure-Class: none`，并说明 fork 自己的提交无 `fix:` |
| `product-file-line-count-ratchet` | ratchet 与落后 1500 提交的 `origin/main` 比较，把上游两周增长算到本 PR 头上 | 用它自带的 `Line-Count-Exception:` 批量豁免（本次 59 条），理由统一为"上游增长、原样导入"；并确认 fork 自己的提交未触碰这些文件 |
| `desktop-changelog-entry` | 上游桌面源码随同步进入 | 加 `desktop/macos/changelog/unreleased/<date>-upstream-sync.json`，内容 `{"kind": "none"}`（2026-08-19 那次同步已用过同一手法） |
| `desktop-e2e-flow-coverage` | 上游新增的 Swift 文件若自带 e2e 覆盖缺口，同步 PR 就会红；该检查**无豁免机制** | **不得编造 e2e 流程**。在正文列出未覆盖文件、注明其为上游新增且 fork 未触碰，作为已知缺口报告 |

生成豁免行的命令：

```bash
scripts/pr-preflight --lane local --base origin/main --pr-body-file <draft>.md 2>&1 \
  | awk '/==> product-file-line-count-ratchet/{f=1} f&&/^- /{print} /<== FAIL product-file-line-count-ratchet/{exit}' \
  | sed -E "s/^- ([^:]+): grew from ([0-9]+) to ([0-9]+) lines.*/Line-Count-Exception: \1 | \2 -> \3 | upstream growth imported by this sync; not authored here/"
```

## 8. `git rerere` 会静默套用旧解法（务必复核）

本仓库 `rerere.enabled=true`，且已积累 152 条缓存解法。后果：一部分冲突会被**自动解决且不留冲突标记**，`grep '<<<<<<<'` 完全查不出来。2026-09-03 的 13 个冲突里有 6 个是这样被处理的。

规则：
- 以 `git status --short` 的 `UU` 为冲突清单，**不要**用 `grep '<<<<<<<'`。
- 合并输出里的 `Resolved '<file>' using previous resolution.` 逐行核对：旧解法未必符合当期策略（例如本次策略是"web/admin 一律取上游"，而缓存里存的是上一次的手工合并结果）。
- 想临时关掉：`git -c rerere.enabled=false merge …`。

## 9. 本机环境注意

- 系统 `python3` 若低于 3.10，`check-manifest-contract` 会因 `.github/scripts/git_bash.py` 的 `str | None` 语法直接崩溃。用 3.12 跑：`ln -sf $(command -v python3.12) /tmp/py312bin/python3 && PATH=/tmp/py312bin:$PATH scripts/pr-preflight …`
- `backend/test.sh` 的 fast-unit CPU 时间守卫（fail 阈值 0.30s）在全量并行下会因机器争用误报。判定方法：单独跑该文件；若通过即为争用，**不要**去改上游的 `tests/fast_unit_duration_allowlist.txt`。

## 10. 推送门禁：本机工具链缺口与格式化陷阱（2026-09-03 实测）

`make setup` 刻意不装 app/desktop 工具链，因此 `git push` 的 pre-push 门禁会在这些阶段卡住。逐个处置（全部是门禁自己披露的开关）：

| 阶段 | 本机原因 | 处置 |
|---|---|---|
| `check_flutter_generated_if_needed` | 本机 Flutter 3.38.9 < 上游 `pubspec.yaml` 要求的 3.47.2，`flutter pub get` 解析失败 | `PRE_PUSH_SKIP_FLUTTER_GENERATED=1`；CI 跑真检查 |
| `dart format --set-exit-if-changed` | 本机 Dart 3.10.8 比上游钉的版本旧，会重排 **289 个中的 180 个上游文件** | `PRE_PUSH_SKIP_DART_FORMAT=1`。**这是本次最危险的一步**：若放任提交，等于重新制造刚从 `web/admin` 清掉的格式化漂移。跳过后务必 `git status` 确认工作区未被改动 |
| `check_windows_kgworker_native_closure_if_needed` | 缺 pnpm 依赖 | `cd desktop/windows && pnpm install --frozen-lockfile`（24 秒），**真跑**而非跳过 |
| `check_desktop_swift_if_needed` | SwiftPM 找不到上游新钉的依赖 commit | 刷新两处缓存仍无效，须删包内工作副本：`rm -rf desktop/macos/Desktop/.build/repositories/<Pkg>-*`，再 `swift package resolve` |
| `pinned backend Python format check` | fork 早期用别的 black 改过 7 个上游文件 | **必须真修**：`scripts/backend-python-format --write <files>`。这里用的是仓库钉住的 black 24.4.2，是权威版本；修完这 7 个文件与上游**完全一致**，冲突面直接减少 |

判定原则：**版本比上游旧的格式化工具一律跳过（否则制造漂移）；仓库钉住的格式化工具一律真跑（它让文件收敛回上游）。** 二者方向相反，不可混为一谈。
