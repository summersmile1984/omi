# 01 · 分支收敛操作手册：两条长期分支 → 一条 `main`

> 输入（2026-09-02 实测）：`main` 的上游基线 2026-08-19（`6ad330a7cf`）；`feature/cloud-neutral-shim` 的上游基线 2026-08-27（`ed5a6fc42c`），fork 专属 321 个提交，触及 932 个文件（**653 个是上游文件**、279 个新文件），其中 main 尚未包含的增量：626 个上游文件改动 + 245 个新文件；`codex/cloudflare-adaptation` fork 专属 762 个提交，777 个新文件、**自身只改 34 个上游文件**。两分支互相合并真实冲突 **20 个文件**；`upstream/main → main` 真实冲突 **13 个**。
> 原则：不做"整分支合并"。把每条分支拆成 ① 接缝（重写为两目标通用，独立 PR）② 纯新增目录（直接检出）③ 不合入项（归档 tag）。全程只在 `main` 上以 PR 推进，不 rebase 公共分支、不 force push。

## 0. 开工前必须定下的决策（见 `07-pr-plan.md` 决策登记）

D1 profile 取值 `omi_cloud|self_hosted|cloudflare` + `stage`；D2 身份契约 v1（`/api/auth`、JWKS 校验、TTL 可配）；D3 Web 运行时保留上游 Next.js（自托管 standalone 容器 / Cloudflare vinext），Moonshine 归档；D4 Cloudflare 未迁移路由的处置（`ORIGIN_BACKEND_URL` 混合 或 能力表 404）；D5 向量维度（Vectorize ≤1536）；D6 品牌与 UUID 分离（白牌 Phase 0）；D7 推送提供方；D8 账户激活围栏默认关闭。

## 1. 冻结与归档（10 分钟）

```bash
git fetch origin upstream --prune
git tag archive/cloudflare-adaptation-2026-09 codex/cloudflare-adaptation
git tag archive/cloud-neutral-shim-2026-09 feature/cloud-neutral-shim
git tag archive/web-moonshine-2026-09 feature/cloud-neutral-shim        # D3：Moonshine 只保留在归档里
git push origin --tags
# 两分支设为只读：GitHub 分支保护"锁定"，PR 全部指向 main
```

## 2. 第 0 步：先把 `main` 同步到上游（≥ shim 的基线）

原因：shim 分支比 main 多合了 8 天上游；先把 main 推到最新，后面所有接缝 PR 都基于同一上游基线，避免同一冲突解两次。

```bash
git worktree add ../wt-sync -b sync/upstream-2026-09-02 main && cd ../wt-sync
git merge-tree --write-tree main upstream/main | grep -c '^CONFLICT'     # 预期 13
git merge --no-ff upstream/main
# 13 个冲突按 06-upstream-sync.md §1 的"永久处置"解决（这次顺手把冲突源消掉）：
#   web/admin ×6 → 取上游（回退 fork 的格式化提交）；guardrail-pulse-history.jsonl → 取上游；
#   .gitignore → 取上游 + 本地条目移到 .git/info/exclude；backend/AGENTS.md → 取上游 + 一行指针；
#   stt_provider_policy.py / stt/streaming.py / cloud_tasks.py → 保留上游新逻辑 + fork 钩子行；
#   test_conversation_notes_v2.py → 取上游，shim 差异用 fork 测试覆盖
make preflight && git commit --no-edit && git push -u origin HEAD && gh pr create ...
```

验收：PR 绿；合并后 `git merge-tree --write-tree main upstream/main | grep -c '^CONFLICT'` 为 0。

## 3. 接缝 PR（S 系列，全部从两分支**抽取并重写**，不整分支合并）

抽取技巧：新文件用 `git checkout <branch> -- <path>`；上游文件的改动用 `git diff main...<branch> -- <path>` 看意图后**手工重写到统一设计**，不要 `git apply` 原 patch（原 patch 带着分支各自的枚举值与路径前缀）。

| PR | 内容 | 来源 | 验收证据 |
|---|---|---|---|
| **S1 profile 源与生成器** | `deploy/profiles/{schema.json,omi_cloud,self_hosted,cloudflare,stages}.yaml`、`scripts/profiles/{render.py,check_tables.py}`、`contracts/profile/fixtures/*.json`；先生成四端表文件但**不接线** | 新写（字段来自 `02-deployment-profile.md` §1；默认值来自两分支现状） | `render.py --target omi_cloud` 产出的 Flutter 表与现有 `environment_profile.dart` 字面量一致（等价回归）；`check_tables.py` 通过 |
| **S2 Flutter 接缝** | 保留 shim 的 `better_auth_client.dart`、`better_auth_session_store.dart`、`firebase_services_policy.dart`、`shared.dart` 刷新逻辑、`auth_service.dart` 围栏；`environment_profile.dart` 改查表并增加 `cloudflare`；`local_wal_sync.dart` 批量读 `profile.sync_upload_batch_limit`；CF 的 `usesBetterAuth`/开发桥仅保留 `stage=local` | shim：`app/lib/services/auth/**`、`app/lib/env/**`、`app/lib/services/auth_service.dart`、`auth_provider.dart`、`preferences.dart`、`backend/http/shared.dart`、`notification_service.dart`、`crashlytics_manager.dart`、`intercom.dart`、`stt_provider.dart`、`connectivity_service.dart`；CF：`local_wal_sync.dart:31` | shim 的 `app/test/unit/env_test.dart` 改读表后通过；`contracts/profile` 三条失败用例（未知 target、配对错误、非 https）通过；`--dart-define=OMI_APP_PROFILE=self_hosted.local` 与 `cloudflare.local` 两种构建均能对本地 Auth 完成登录→刷新→登出 |
| **S3 桌面接缝** | Windows：`src/shared/deploymentProfile.ts` 增加 `'cloudflare'`、新建 `src/main/config/deployment.ts` 统一读取（消灭 15 处散落的 `import.meta.env.VITE_*` 与两处 `api.omi.me` 兜底）；macOS：`DesktopDeploymentProfile` 增加 `.cloudflare`，`DesktopBackendEnvironment` 四个 URL 改查表，`DesktopModelEgressPolicy` 读能力开关；合入 CF 的 `CalendarReaderService.swift` 走 Worker 版本但以 `allow_google_connectors` 门控 | shim：`desktop/windows/src/shared/deploymentProfile.ts` 及其 58 处消费者中属于围栏的部分、`desktop/macos/Desktop/Sources/DesktopBackendEnvironment.swift`；CF：`CalendarReaderService.swift`、`ConnectorImportOperations.swift`、`SettingsContentView+Integrations.swift`、`APIClient+Calendar.swift` + 2 个测试 | Windows `deploymentProfile.test.ts` 与 macOS `DesktopBackendEnvironment` 测试通过；`OMI_APP_NAME=omi-profile-cf ./run.sh` 用 `cloudflare.local` 表连上本地 `wrangler dev` 完成登录 |
| **S4 Web 接缝（基于上游 Next.js，D3）** | 新建 `web/app/src/lib/deploymentProfile.ts` + 生成表；`firebase.ts` 改读 `identityProvider`；合入 CF 的 `auth-proxy.ts`（运行时无关）、`src/app/api/better-auth/[...path]/route.ts`（路径改 `/api/auth`）、`realtimeTicket.ts`、`transcriptionSocket.ts` 票据首帧、`AuthProvider.tsx`/`LoginPanel.tsx` 双模式；`NEXT_PUBLIC_AUTH_MODE` 退役；vinext 文件（`vite.config.ts`、`wrangler.jsonc`、`next.config.js` 条件别名、`package.json` 脚本）作为加法合入 | CF：`web/app` 的 16 个修改 + 35 个新增文件；**不取** shim 的 Moonshine 改动 | `web-checks.yml` 绿（上游 Next 构建仍可用）；`npm run build:vinext:staging` 通过；对自托管 Next standalone 与 `wrangler dev` 两种运行时，同一套 Playwright 登录用例通过 |
| **S5 后端接缝** | shim 的 `utils/identity.py`、`auth_shim.py`（已在 main 的版本升级）、`config/push_provider.py`、`push_webhook.py`、`storage_minio.py`、`cloud_tasks_redis.py`、`egress_policy.py`、`database/__init__.py`（`FIRESTORE_PG_DSN` 早期安装）；`OMI_DEPLOYMENT_PROFILE` 值统一；上游文件注入点最小化（`_client.py`、`endpoints.py`、`storage.py`、`cloud_tasks.py`、`main.py`、`dependencies.py` 各 ≤5 行钩子）；STT/翻译 provider 改注册表（`utils/stt/providers_fork/`）；CF 的 `rate_limit_config.py` +12 策略、`export_openapi.py` 的 `cloudflare-route-inventory` 面与 3 个测试；新增 `routers/fork/realtime_ticket.py` | shim：上述文件；CF：`rate_limit_config.py`、`export_openapi.py`、`test_openapi_contract.py`、`transcription_capability_probe.py`（通用则收） | `backend/test.sh` 绿；`fork-upstream-touch` 报告每个上游文件 ≤5 行；`AUTH_PROVIDER=better_auth` 与 `firebase` 两种启动都通过 `validate_identity_configuration`；`export_openapi.py --surface cloudflare-route-inventory --check` 通过 |
| **S6 共享认证包** | `auth/shared/`（TS）：JWT 参数、claims、Firebase scrypt 校验与首登重哈希、capabilities 端点；`auth-server/` 改为 Express adapter 引用它；TTL/`iss`/`aud` 读配置 | shim：`auth-server/src/{auth.js,firebase-migration-password.js,import-firebase-users.js}`；CF：`workers/auth/index.ts:110-241` 的对应逻辑 | `contracts/auth/` 套件对 `auth-server` 全绿（sign-up→sign-in→token→JWKS→刷新→sign-out→cookie→web-ticket） |

顺序：S1 → S2/S3/S5 并行 → S4、S6 → 进入 M 系列。S 系列合并后 `main` 已经能以 `self_hosted` profile 跑通端到端（后端用 main 上已有的 shim + S5）。

## 4. M1：合入自托管余量（几乎全是新文件）

```bash
git switch -c merge/self-host-remainder main
# 新增目录整体检出（来自归档 tag，避免分支再动）
git checkout archive/cloud-neutral-shim-2026-09 -- \
  deploy/self-host backend/firestore_pg backend/utils/mimo_pipeline backend/utils/moss_pipeline \
  backend/utils/sensevoice backend/utils/mlx_moss_diarize auth-server \
  docs/product/invariants/deployment-model-neutrality.md docs/product/invariants/task-capture-suggestion-only.md \
  docs/epics/deployment_model_neutrality.md docs/architecture/deployment-neutral-* docs/runbooks/terminal-to-pc-claude-codex.md
```

对 shim 分支剩余的 **626 个上游文件改动**按类别处置（用 `git log --no-merges --format='%h %s' upstream/main..archive/cloud-neutral-shim-2026-09 -- <path>` 看每处意图）：

| 类别（文件数） | 处置 |
|---|---|
| `desktop/windows/src`（130）：self-host 围栏、模型能力 IPC、身份/出站边界 | 围栏部分已在 S3；其余属于**功能**（Windows 模型能力桥、egress 边界）→ 逐提交评审，保留通用的，作为独立 PR `M1-win` 合入 |
| `app/lib/l10n`（99）：ARB + 生成文件 | **不合并**；新增/改动的 key 在 ARB 中重放，`gen-l10n` 重新生成（生成文件永不手工合并） |
| `backend/tests/unit`（83）+ `backend/tests/integration`（3）：修改上游测试以适配 shim | **不合并**上游测试改动；把等价断言写进 `backend/tests/unit/fork/`（S5 已建目录） |
| `desktop/macos/Desktop`（68）：self-host 围栏与 Better Auth 登录 | 围栏在 S3；`AuthService.swift` 等登录改动按契约 v1 重写为独立 PR `M1-mac` |
| `desktop/context-for-claude`（11+5+2+2）：姊妹应用的自托管适配 | 独立 PR `M1-ctx`，按 profile 表重写 |
| `backend/utils/*`（retrieval 8、llm 7、stt 6、other 4、conversations 4、translation_core 2、memory 2）、`backend/routers/listen`（2）、`services/users`（2）、`llm_gateway/routers`（2） | 属于 provider 替换与 shim 注入 → S5 已覆盖注入点；provider 实现随 M1 新目录进入；其余逐文件评审，超过 5 行的改动重构为钩子 |
| 上游不变量文档修改（11 个） | **不合并**；差异写成 `docs/product/invariants/fork/*.md` 增补 |
| `.github/scripts`（5）、`.github/workflows/gcp_backend_pusher_auto_deploy.yml`、`config/deployment-setting-classification.json` | 不改上游 CI 文件；等价内容进 `checks-manifest.fork.yaml` / `*.fork.json` |
| 格式化专用提交（`style(...)`） | 一律丢弃 |

验收：`fork-contract-selfhost.yml` 绿（`compose.ci.yml` 起栈 + 契约套件 + `auth-flow-smoke.py`）；`deploy/self-host/operations.sh self-check` 通过；`git merge-tree` 对 `upstream/main` 的真实冲突数不高于第 0 步之后的值。

## 5. M2：合入 Cloudflare（新增目录 + 34 个上游文件改动重写）

```bash
git switch -c merge/cloudflare main
git checkout archive/cloudflare-adaptation-2026-09 -- deploy/cloudflare docs/cloudflare-architecture \
  dev/cloudflare-staging-validation-plan.md docs/agents/fallback-telemetry.md docs/doc/developer/desktop-updates.mdx \
  docs/doc/developer/gmail-calendar-oauth-migration.md
git mv .github/scripts/<cf 新增的 4 个脚本> scripts/fork/            # 不往上游目录加文件
```

CF 分支 34 个上游文件改动的处置：

| 文件 | 处置 |
|---|---|
| `app/lib/services/auth_service.dart`、`auth_provider.dart`、`backend/preferences.dart` | **丢弃**（S2 的 shim 客户端取代开发桥） |
| `app/lib/services/wals/local_wal_sync.dart` + 测试 | 已在 S2 改为读 profile |
| `web/app` 16 个文件 | 已在 S4 合入 |
| `backend/utils/rate_limit_config.py`、`scripts/export_openapi.py`、`tests/unit/test_openapi_contract.py`、`scripts/transcription_capability_probe.py` + 测试 | 已在 S5 |
| `backend/deploy/runtime_env/_base.yaml`（仅注释） | 丢弃 |
| `.github/checks-manifest.yaml`（3 条 CF 检查）、`.gitignore` | 移到 `checks-manifest.fork.yaml` / `deploy/cloudflare/.gitignore` |
| `desktop/macos` 5 个日历文件 + `e2e/flows/google-connector-read.yaml` | 已在 S3 |
| `docs/agents/fallback-telemetry.md`、`docs/doc/*` 3 个 | 直接合入（文档） |

随后在同一 PR 系列内完成 CF 目录自身的契约对齐（`03-deploy-targets.md` §3）：Auth Worker `basePath=/api/auth`、TTL/`iss`/`aud` 读 vars、Edge 对 bearer JWT 用 JWKS、`account_activation_fence` 读 profile、`omi-cf-*` 资源名改由品牌渲染、`LEGACY_BACKEND_URL`→`ORIGIN_BACKEND_URL`。

验收：`fork-contract-cloudflare.yml` 绿（`wrangler dev` + 同一契约套件）；`npm test`（104 TS）与 `uv run pytest`（70 Python）绿；`export_openapi.py --surface cloudflare-route-inventory --check` 绿；S2 的 Flutter 构建以 `cloudflare.local` 表完成登录→录音→对话闭环。

## 6. 20 个互冲突文件的最终归属

| 文件 | 归属 |
|---|---|
| `app/lib/services/auth_service.dart`、`auth_provider.dart`、`backend/preferences.dart` | S2（shim 实现为主体） |
| `web/app/src/lib/api.ts`、`firebase.ts`、`components/auth/LoginPanel.tsx`、`app/login/LoginClient.tsx`、`app/api/proxy/[...path]/route.ts`、`components/settings/SettingsPage.tsx`、`components/conversations/GenerateSummaryButton.tsx`、`components/public-build-canary.tsx`、`web/app/package.json`、`web/app/.gitignore` | S4（CF 实现为基础，接 profile） |
| `web/app/next.config.js`、`next-env.d.ts`、`package-lock.json`（shim 删除 / CF 修改） | S4 保留上游文件 + CF 加法（D3） |
| `desktop/macos/Desktop/Sources/CalendarReaderService.swift` | S3（CF 版本 + 能力门控） |
| `.github/checks-manifest.yaml`、`.gitignore`、`.github/guardrail-pulse-history.jsonl` | 上游版本；fork 条目进 fork 文件 |

## 7. M3/M4：CI 矩阵与收尾

- M3：按 `05-ci-matrix.md` C0–C6 落地；两目标契约工作流与构建矩阵在 `main` 上全绿一周。
- M4：删除 `codex/cloudflare-adaptation` 与 `feature/cloud-neutral-shim`（归档 tag 已在）；`AGENTS.fork.md` 写入"禁止长期目标分支；部署目标 = `deploy/<target>/` + profile；品牌 = `brand/<id>/`"；`dev/cloud-neutral-overview.md` 与 `dev/cloudflare-adaptation-plan.md` 顶部加"已被 `dev/unified-main/` 取代"的说明并保留历史。

## 8. 每步的门禁命令（复制即用）

```bash
# 上游冲突数（每个 PR 合并后都跑一次，写进 sync-log.md）
git merge-tree --write-tree main upstream/main | grep -c '^CONFLICT'
# 上游文件被 fork 触碰的数量（应单调下降）
comm -12 <(git log --no-merges --name-only --format= upstream/main..main | sort -u) <(git ls-tree -r --name-only upstream/main | sort) | wc -l
# 两套门禁
make preflight && scripts/fork/preflight
# 两目标契约（本地）
deploy/self-host/ci/contract.sh && deploy/cloudflare/ci/contract.sh
```

## 9. 回滚

一切经 PR 进 `main`，任一 PR 可 `git revert -m 1`；归档 tag 保证两条旧分支的任何状态可恢复；不删除任何上游文件、不 force push，所以上游同步永远可继续。
