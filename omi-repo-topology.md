# 仓库拓扑方案：一个 fork、一条主线、两个部署目标、N 个品牌

> 日期：2026-09-02 · 基线：`main` 7f6e8ef7aa、`codex/cloudflare-adaptation` 65225401c8、`feature/cloud-neutral-shim` 2aba718423、`upstream/main`
> 问题：Cloudflare 全托管与普通服务器自托管两条线，是长期双分支、拆两个 repo 各自 fork 上游，还是别的？上游是前后端一体的 monorepo，还要叠加白牌化。
> 关联：`omi-white-label-strategy.md`（品牌维度）、`omi-cloud-neutral-postgres-migration.md`、`dev/cloudflare-adaptation-plan.md`、`dev/cloud-neutral-overview.md`。

---

## 总：结论

**不要双分支，也不要拆 repo。保持一个 fork、一条 `main`，把"部署目标"和"品牌"都做成目录 + 配置维度，而不是分支维度。** 当前两条分支其实已经是这个形态的雏形：Cloudflare 的 616 个文件全部在 `deploy/cloudflare/`，自托管的 24 个文件在 `deploy/self-host/`，二者对上游代码的真正改动只集中在**同一条接缝**（客户端认证、Web 构建配置、限流配置、检查清单）上。把这条接缝在 `main` 上设计一次，两条分支就可以合并并删除。

三个数字说明为什么：

| 度量（今天实测，`git merge-tree` 干跑，只计真实冲突文件，不含自动合并成功的文件） | 结果 |
|---|---|
| 把 `upstream/main` 合进 `main` 的冲突文件数 | 13 |
| 合进 `codex/cloudflare-adaptation` | 30 |
| 合进 `feature/cloud-neutral-shim` | 46 |
| 两条分支互相合并的冲突文件数 | 20 |

双分支意味着每次同步上游付 30 + 46 的代价，而且每个通用修复都要 cherry-pick 两次；拆两个 repo 是同样的代价再加上丢失跨组件原子提交与共享 CI。单主线只付一次 13（且可以压到 5 以下，见"减冲突战术"）。

## 分一：两条分支到底分叉在哪里

| 分支 | 相对上游的 fork 专属提交 | 新增文件 | 修改的上游文件 | 性质 |
|---|---|---|---|---|
| `main` | 73 | 75（`backend/firestore_pg/` 11、`backend/utils/` 16、根级 `omi-*.md`） | 60 | 上游 + 早期 shim（PR #1 已落地） |
| `codex/cloudflare-adaptation` | 762 | 777（`deploy/cloudflare/` 618、`docs/cloudflare-architecture/` 33、`web/app` 35） | 92（其中 60 继承自 main，**自身只改了约 40 个**） | **Worker-first 重新实现**：TS Edge/Auth/Jobs/Realtime/RateLimit Workers + Python Workers（`api-core`/`api-ai`），D1/R2/Queues/DO/Vectorize/Workers AI；`deploy/cloudflare/python` 174 个 py 文件**零 import `backend/`**，按设计不共享后端代码，只共享 API 契约与客户端 |
| `feature/cloud-neutral-shim` | 321 | 279（`deploy/self-host/` 24、`backend/firestore_pg/` 16、`auth-server/`、MiMo/MOSS/SenseVoice 管线） | 653（`app/lib` 139、`desktop/windows` 137、`desktop/macos` 80、`backend/utils` 49、`backend/routers` 26…） | 后端走 **shim**（`firestore_pg`、`auth_shim`、`storage_minio`、`cloud_tasks_redis`，"上游只改配置"）；但客户端侧大面积改动，主题是"**按部署 profile 围栏 Firebase/Crashlytics/推送/Google 连接器**"（194 个 fix 提交） |

两条分支不互相包含；`main` 领先 shim 分支仅 2 个提交（每周 pulse），即 shim 分支 ≈ main 的超集 + 更新的上游。

**20 个互相冲突的文件是同一条接缝**（各自改法不同，目的相同）：

| 文件 | Cloudflare 改动 | 自托管改动 | 共同目的 |
|---|---|---|---|
| `app/lib/services/auth_service.dart` | +88/−42 | +184/−7 | Better Auth 登录、JWT 携带 |
| `app/lib/providers/auth_provider.dart` | +35/−5 | +69/−70 | 同上 |
| `app/lib/backend/preferences.dart` | +7/−0 | +129/−4 | 部署 profile 持久化 |
| `web/app/src/lib/api.ts` | +154/−163 | +397/−18 | API 基址与鉴权头 |
| `web/app/src/lib/firebase.ts`、`LoginPanel.tsx` | Workers 运行时 | 自托管运行时 | 去 Firebase 登录 |
| `web/app/next.config.js`、`package.json`、`next-env.d.ts` | 改为 Workers/vite 构建 | **删除**（modify/delete 冲突） | Web 构建目标 |
| `backend/utils/rate_limit_config.py` | +24/−1 | +53/−4 | 限流参数外置 |
| `.github/checks-manifest.yaml`、`.gitignore`、`guardrail-pulse-history.jsonl` | 各加检查 | 各加检查 | CI 清单 |

结论：冲突不是"两个部署平台天然不兼容"，而是**同一个抽象（部署 profile + 认证契约）被在两个分支上各写了一遍**。

## 分二：目标拓扑

```
memweft（fork of BasedHardware/omi，单一 main）
├── app/  desktop/  web/         # 上游客户端；fork 只通过"部署 profile 接缝"和"品牌注入点"改动
├── backend/                     # 上游 Python 单体 + 自托管 shim（firestore_pg / auth_shim / storage_minio / cloud_tasks_redis）
├── contracts/                   # 共享契约：OpenAPI、WebSocket 协议、parity 夹具 —— 对两个后端实现都跑一致性套件
├── deploy/
│   ├── self-host/               # compose / nginx / 验收脚本（已有，24 文件）
│   └── cloudflare/              # Workers 实现 + wrangler + 迁移（已有，616 文件；是第二个后端实现，不是第二个产品）
├── auth-server/                 # Better Auth 配置为一份；两个 adapter：Express+PG（自托管）、Hono+D1 Worker（Cloudflare）
├── brand/                       # 品牌清单（白牌维度，见 omi-white-label-strategy.md）
└── .github/workflows/           # 上游 GCP 工作流原样保留、在 GitHub 界面禁用；fork 新增 deploy-selfhost.yml / deploy-cloudflare.yml
```

三个维度，全部是配置而非分支：

| 维度 | 取值 | 作用面 | 形式 |
|---|---|---|---|
| **部署目标** `DEPLOY_TARGET` | `gcp-upstream` / `selfhost` / `cloudflare` | 后端实现选择、CI 部署矩阵 | 目录 + 环境变量 |
| **客户端 profile** `OMI_APP_PROFILE` 扩展 | 同上三值 | API 基址、认证提供方（Firebase vs Better Auth JWKS）、推送、分析、Crashlytics、Google 连接器可用性、对象存储 URL 形状 | 一份 `deployment/profile.{dart,ts,swift}` 抽象，替代今天两分支各自的围栏代码 |
| **品牌** | `brand/<name>/manifest.yaml` | 显示名、Bundle ID、图标、文案、AI 自称、域名 | 构建期生成物 |

关键设计原则：**客户端不应知道后端跑在 4C8G 上还是 Worker 上。** 它只知道 profile 给的 API 基址、JWKS 地址与能力开关。Better Auth 在两处的**契约必须相同**（ES256 JWT、`uid` claim、同一 JWKS 路径、同一刷新语义），这样 `auth_service.dart` 只有一份。

**Cloudflare 与自托管可以互补而非互斥**：Edge Worker 已经设计了"按路由回退到旧后端"。把回退目标从 GCP 旧后端换成自托管 Python 单体（容器起在任意服务器，Cloudflare 作为其前置边缘），未迁移到 Workers 的长尾路由自动由同一份 `backend/` 服务——这是单仓库单契约才有的选项。

## 分三：为什么不是另外两条路

**长期双分支**：每次上游同步付两倍冲突（今天 30 + 46）；通用修复（品牌、安全、STT 供应商）要 cherry-pick 两遍并各自验证；接缝代码天然分叉（已经发生：20 文件）；白牌再叠一层就是 2 × N 个分支。

**拆两个 repo 各自 fork 上游**：双分支的全部缺点，再加上：丢失上游跨组件原子变更（上游经常一个 PR 同时改 `app/`+`backend/`+`contracts/parity`，拆 repo 后要靠 `git subtree split` 逐次对齐）；两套 CI、两套 `AGENTS.md`、两套检查清单；客户端要么复制两份要么做成第三个 repo。上游是 monorepo 这件事对 fork 是**优势**（原子性），不是负担；大仓库的开发体验用 `git sparse-checkout`（cone 模式，按角色只检出 `app/` 或 `backend/`）+ 每任务一个 worktree（已在用）+ CI 路径过滤（上游已有 `detect-changes` action）解决。

**何时才该拆 repo**：① Cloudflare 实现的客户端开始与上游客户端分叉成另一个产品；② 两条线由不同组织维护、需要不同的访问控制；③ 一条要开源一条要闭源。目前三者都不成立。若只是"品牌资产/部署密钥不想放在公开 fork 里"，用一个小的**私有 overlay repo**（只含 `brand/<name>/`、`deploy/*/secrets`、法律文本），由 CI 在构建时拉取，代码仍在同一个 fork。

## 分四：把两条分支收敛到一条主线（2~3 周，一人）

1. **冻结**：两分支停止接新功能，打 tag（`archive/cloudflare-2026-09`、`archive/self-host-2026-09`）。
2. **先落接缝**（从 shim 分支抽取，重写为两目标通用，独立 PR 进 `main`）：客户端部署 profile 抽象（Flutter/Web/桌面）、Better Auth 契约与 `auth_service.dart` 单一实现、`rate_limit_config.py` 外置、`checks-manifest.yaml` 的 fork 检查条目。验收：同一客户端二进制仅靠 profile 切换即可登录自托管与 Cloudflare 两个 staging。
3. **再合自托管**：`deploy/self-host/`、`backend/firestore_pg/` 补齐（main 上 11 个文件 → 16）、`auth-server/`、STT 管线（MiMo/MOSS/SenseVoice）→ `main`。这些几乎全是新增文件，冲突极少。
4. **最后合 Cloudflare**：`deploy/cloudflare/` 与 `docs/cloudflare-architecture/` 整体新增；其对约 40 个共享文件的改动**重写到新接缝上**（分支自己的计划书就写了"选择性移植认证契约，不能整分支合并"）；`web/app` 的 Workers 构建改为 profile/target 条件化而非替换 Next.js 配置。预期冲突范围就是那 20 个文件，只付一次。
5. **CI 矩阵**：`target ∈ {selfhost, cloudflare}` × `brand ∈ {…}`；`contracts/` 一致性套件对两个后端都跑（自托管用 compose，Cloudflare 用 `wrangler dev`）。
6. **删除长期分支**，在 `AGENTS.md`（fork 段）写入规则：**不允许长期目标分支；部署目标 = 目录 + profile。**

## 分五：与上游同步的常态流程与减冲突战术

- **节律**：每周一次 `upstream/main → main` 合并（merge 而非 rebase，保留历史、不 force push），由定时工作流开 "upstream-sync" PR，人只解冲突。
- **"不修改上游文件"清单**（今天 13 个冲突文件里的大头，全部可消）：
  - `.github/guardrail-pulse-history.jsonl`：fork 的每周 pulse 机器人与上游的机器人各自提交同一文件，**每周必冲突** → 在 fork 里禁用该工作流，或让它写 fork 专属文件。
  - `backend/pylock*.toml`、`requirements.txt`：fork 依赖放 `backend/requirements-fork.txt`，由 `deploy/self-host` 的 Dockerfile 叠加安装，不动上游锁文件。
  - `AGENTS.md`、`backend/AGENTS.md`：fork 规则放 `AGENTS.fork.md`，上游文件里只加一行指针。
  - `.github/checks-manifest.yaml`：fork 检查放 `checks-manifest.fork.yaml`，运行器合并读取（一次性小改，可回推上游）。
  - `.gitignore`：用 `.git/info/exclude` 或目录级 `.gitignore`（`deploy/*/.gitignore`）。
  - **格式化提交**：`main` 上 13 个冲突里有 6 个（`web/admin/**`）来自 fork 自己的 `style(admin): format …` 提交——pre-commit 钩子用与上游不同版本的 prettier/black 重排了上游文件。规则：**永不提交只含格式变化的上游文件**；把 fork 的格式化工具版本钉到与上游 `.pre-commit-config.yaml` 一致，钩子只格式化 fork 自有路径。
  - GCP 工作流：**不删**（删除 = 每次上游改动都冲突），在 GitHub Actions 界面禁用即可，零文件改动。
- **接缝改动尽量回推上游**：部署 profile 抽象、认证提供方接口、限流参数外置、`{product_name}` 插值，对上游都是无害的可配置化；每接受一个，fork 的长期冲突面就少一块。
- **度量**：每次同步记录冲突文件数；目标从 13 降到 5 以下并保持；若某次超过 15，先找"谁又修改了上游文件"。

## 总：一句话

**分支是用来做"短暂的工作副本"的，不是用来表达"产品有几种部署方式"的。** 部署方式与品牌都是矩阵维度，矩阵用目录和配置表达，由一条 `main` 承载、由一次上游合并喂养。当前两条分支的 640 个新增文件都已经在正确的目录里，缺的只是把那 20 个文件的接缝设计一次；做完这件事，"维护两条线"就变成"维护一条线上的两个目录"。
