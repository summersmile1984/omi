# 09 · 上游分歧文件清单（每次同步前生成、同步后更新）

`06-upstream-sync.md` §1 曾经写过"今天的 13 个冲突文件与永久处置"，把它当成一次性快照处理：写完表格、合并、就再没人回来对过账。这份文档把它换成一个**每次同步都重新生成、且必须手工核对更新**的活清单——回答的问题是"fork 现在到底改了哪些上游文件、为什么、打算怎么消化、消化了没有"，而不是"上次同步遇到了什么"。

## 怎么生成这份清单（机械、可重复）

`scripts/fork/check-upstream-touch.py` 已经是判定"这个改动算不算碰了上游文件"的唯一权威实现（每个 PR 的 CI 门禁用的就是它）。生成本清单直接复用它，只是把对比范围从"这个 PR 相对 origin/main"换成"fork 相对上一次同步基点，累计做了什么"：

```bash
# 1. 找到 fork 与上游最后一次共同祖先(= 上一次同步真正拉到的上游提交)
MB=$(git merge-base upstream/main origin/main)

# 2. 用同一个检查器,把"这个 PR 的 diff"换成"fork 自己这些提交的累计 diff"
python3 scripts/fork/check-upstream-touch.py \
  --base "$MB" --head origin/main --upstream-ref upstream/main --json \
  > /tmp/upstream-touch-full.json

# 3. violations 数组就是下面这张表的原始数据源;allowed 数组是已经在
#    upstream-touch-allowlist.yaml 里正确登记、按预算走的 T1 缝,不算债务。
```

**不要**用 `git diff upstream/main origin/main`(两个独立前进的分支互相比)——那样会把"上游这段时间自己改了什么"和"fork 真正改了什么"混在一起,规模会被严重高估(2026-09-04 实测：这样算出 523 个"不同"的文件,而下面这张真实、准确的表只有 40 个,其中 38 个是真违规——这两个数字本身都会随时间漂移,不必追求跟当次复现完全一致,重点是"混算"和"只算 fork 自己动过的"之间那个数量级差距)。也不要用不带 remote 前缀的裸 `main`——本地 `main` ref 可能是过期的旧指针,不代表 `origin/main`,这份清单最早的一版就因为这个踩了坑,把冲突数错报成 14(应为 2)。

## 现状快照(2026-09-04,merge-base `fd01c27267`)

- 上游领先 270 个提交,fork 领先 134 个(`git rev-list --left-right --count upstream/main...origin/main`)。
- `git merge-tree --write-tree origin/main upstream/main` 真实合并冲突:**2 个**——`backend/testing/desktop_beta_admission/run.sh`(T1 白名单内、预算内,见下方"未计入债务"）与 `backend/utils/llm/model_config.py`(债务,见下表)。冲突数会随上游下一次恰好碰到哪些文件而波动,**不是**本清单要跟踪的指标。
- `check-upstream-touch.py` 累计核算:**40 个上游文件被 fork 动过**,其中 **2 个**在 `upstream-touch-allowlist.yaml` 里正确登记(在预算内,不算债务),**38 个**是本清单要跟踪的真实债务。

## 未消化的分歧(37 个,按子系统分组;第 38 个是 `AuthService.swift`,单独处理见下方)

状态列:`待处置`(已经有一个可以直接照做的处置方案,不管背后那次改动的源头提交是否已经追溯到)· `待诊断`(处置方案本身还没想清楚——通常是因为不确定具体改了什么、影响面多大,需要有人接手前先查清楚才能定处置方案)。两者都不代表"原因"列一定写了具体的源头提交:"原因"列的 commit 是 `check-upstream-touch.py` 报告里离 HEAD 最近的一次改动,不一定是最初引入分歧的那次;标了 `合并提交` 的还没往前追溯到真正的源头提交,但这不影响处置方案是否已经明确。

### A. Backend STT/翻译 provider 扩展(cloud-neutral 系列,8 个)
上游对应文件本该保持零改动,provider 实现应迁到 `backend/fork/`、用导入期补丁挂载(`06-upstream-sync.md` §1 当初就是这么写的,但从没真正执行)。

| 文件 | 原因 | 处置方案 | 状态 |
|---|---|---|---|
| `backend/config/prerecorded_stt.py` | `9b8ba655bf` feat(cloud-neutral): align adapters with upstream contracts | 迁到 `backend/fork/stt/`,补丁注册表在导入时挂载 | 待处置 |
| `backend/config/stt_provider_policy.py` | 合并提交,源头需要追溯 | 同上 | 待处置 |
| `backend/utils/stt/pre_recorded.py` | 合并提交,源头需要追溯 | 同上 | 待处置 |
| `backend/utils/stt/streaming.py` | 合并提交,源头需要追溯 | 同上 | 待处置 |
| `backend/utils/llm/model_config.py` | 合并提交;`17460648cd` feat(llm): configurable translation provider — MiMo/DeepSeek/Gemini 是可能的源头 | 同上 | 待处置 |
| `backend/utils/llm/providers.py` | `76468f50be` Merge cloud-neutral shim onto upstream main | 同上 | 待处置 |
| `backend/utils/translation_core/providers.py` | 合并提交,源头需要追溯 | 同上 | 待处置 |
| `backend/utils/other/endpoints.py` | 合并提交,源头需要追溯 | 同上 | 待处置 |

对应测试(同一批 provider 工作带出来的,**不能**直接改上游测试——上游测试要保持不动,fork 行为要在 fork 自己的测试目录里断言):

| 文件 | 原因 | 处置方案 | 状态 |
|---|---|---|---|
| `backend/tests/unit/test_prerecorded_stt_config.py` | `9b8ba655bf` | 上游测试恢复原样;fork 行为的断言挪到 `backend/tests/unit/fork/` | 待处置 |
| `backend/tests/unit/test_stt_provider_policy.py` | 合并提交,源头需要追溯 | 同上 | 待处置 |
| `backend/tests/unit/test_agent_vm_firebase_project_split.py` | `9b8ba655bf` | 同上 | 待处置 |
| `backend/tests/unit/test_language_catalog.py` | `7b08862efe` style(backend): apply pinned formatting——**只是格式化漂移**,跟 `web/admin` 那 3 个 prettier 提交是同一类问题 | 直接恢复上游字节,钉住格式化工具版本(见 `06-upstream-sync.md` 旧 §1 关于 web/admin 的处置) | 待处置(比其它几条简单——不涉及逻辑,纯还原) |

### B. Backend 云中立基建(self-host 部署核心,4 个)
M1(自托管部署)的直接产物,`c6fc05dd70` 已经把 `storage_minio.py` 这类**新增**文件迁出了上游包,但下面这几个**上游自己的文件**还留着改动——说明那次"迁移"做了一半。

| 文件 | 原因 | 处置方案 | 状态 |
|---|---|---|---|
| `backend/utils/other/storage.py` | `c6fc05dd70` 之后仍有残留改动(该提交只迁出了新增的 `storage_minio.py`,没有把这个上游文件本身清零) | 确认上游文件里具体还剩什么 hook,收尾迁移 | 待诊断 |
| `backend/utils/cloud_tasks.py` | `83e627b428` feat(queue): Redis task queue shim — Cloud Tasks replacement for local dev(`06-upstream-sync.md` 旧 §1 就记录过这条,一直没执行) | `backend/fork/patches/queue.py` 在导入时替换 `utils.cloud_tasks` 的派发函数,实现挪到 `backend/fork/cloud_tasks_redis.py`;上游文件零改动 | 待处置 |
| `backend/database/__init__.py` | `f259167751` chore(cloud-neutral): preserve optional database imports | 迁到条件导入的 fork 补丁,而不是改上游 `__init__.py` | 待处置 |
| `backend/database/_client.py` | 合并提交,源头需要追溯 | 同上 | 待诊断 |

### C. Backend 打包/依赖清单(10 个)
锁文件与 Dockerfile 这类"生成物"本来就在 T2 永不可改清单里——这批全都需要一个不同的解法(独立 pusher 镜像/依赖树),而不是让 upstream-touch-allowlist 破例。

| 文件 | 原因 | 处置方案 | 状态 |
|---|---|---|---|
| `backend/modal/Dockerfile` | `c6fc05dd70` | 需要 fork 自己的 Dockerfile 变体,而不是改上游那份 | 待诊断 |
| `backend/pusher/Dockerfile` | `c6fc05dd70` | 同上 | 待诊断 |
| `backend/pusher/pylock.toml` / `requirements.txt` | 合并提交,大概率是 fork 加了 provider SDK 依赖后锁文件跟着变 | 需要判断这些依赖能不能只加在 fork 自己的 extra/optional-dependency 分组 | 待诊断 |
| `backend/pylock.{toml,macos.toml,macos-x86_64.toml,runtime.toml,windows.toml}` / `requirements.txt` | 同上 | 同上 | 待诊断 |

### D. Backend routers(3 个)+ 相关测试(2 个)
| 文件 | 原因 | 处置方案 | 状态 |
|---|---|---|---|
| `backend/routers/desktop_tts_updates.py` | 合并提交,源头需要追溯 | 待诊断具体改了什么 | 待诊断 |
| `backend/routers/listen/receiver.py` | 合并提交,源头需要追溯 | 同上 | 待诊断 |
| `backend/routers/tts.py` | `024e2d3527` chore(backend): satisfy cloud adapter type checks——听起来是类型检查相关的小改动 | 待诊断能否收窄成一个补丁点 | 待诊断 |
| `backend/tests/unit/test_pusher_auto_deploy_paths.py` | `c6fc05dd70` | 上游测试恢复原样,fork 断言挪到 fork 测试目录 | 待处置 |
| `backend/tests/unit/test_verify_pusher_source_closure.py` | `c6fc05dd70` | 同上 | 待处置 |

### E. CI 工作流(1 个)
| 文件 | 原因 | 处置方案 | 状态 |
|---|---|---|---|
| `.github/workflows/gcp_backend_pusher_auto_deploy.yml` | `c6fc05dd70` | 工具自己给的处置就是标准做法:fork 工作流改用新的 `.github/workflows/fork-*.yml`,上游那份在 fork 里禁用、不编辑 | 待处置(方法已知,只是没做) |

### F. Mobile(Flutter,3 个)
B2(移动端身份注入,`dev/unified-main/04-brand-layer.md` B2 行)正式依赖 S2(Flutter 客户端接线),S2 还没做——这几个大概率是 S2 该收口的范围。

| 文件 | 原因 | 处置方案 | 状态 |
|---|---|---|---|
| `app/lib/pages/onboarding/auth.dart` | `a5ed639dda` chore(auth): secure self-hosted token bridges | 待判断能否收窄成一个读取 fork 配置的小缝 | 待诊断 |
| `app/lib/providers/auth_provider.dart` | 合并提交,源头需要追溯 | 同上 | 待诊断 |
| `app/lib/pages/onboarding/primary_language/primary_language_widget.dart` | `974af498a2` style(app): format primary language lookup——**只是格式化漂移** | 直接恢复上游字节,钉住格式化工具版本 | 待处置(纯还原,不涉及逻辑) |

### G. 其它(2 个)
| 文件 | 原因 | 处置方案 | 状态 |
|---|---|---|---|
| `Makefile` | `9803b46575` dev: shadow-diff regression lane (`make dev-shadow-diff`) | 这是纯本地开发工具目标,应该挪到 fork 自己的 `dev/Makefile` 或独立脚本,`make` 用 `-f` 或 `include` 组合,不改上游根 `Makefile` | 待处置 |
| `docs/api-reference/app-client-openapi.json` | 合并提交;大概率是从后端路由自动生成的产物,fork 路由差异导致输出跟着变 | 如果确认是生成物,应该加进 T2 生成文件清单而不是当成"手改"违规追责;需要先确认生成脚本 | 待诊断 |

## 已知但还没被本清单收录的一条(需要单独一次审计)

`desktop/macos/Desktop/Sources/AuthService.swift` 目前 `check-upstream-touch.py` 报的是 **over-budget**(累计新增 9 行,`upstream-touch-allowlist.yaml` 里登记的预算是 3 行),不是"未登记"。这条不是本清单的"未消化分歧"类别——它是一个**已登记但预算写小了、或者缝本身长歪了**的问题,即预算是在某一次 PR 里对着当时的 `origin/main`校验通过的,后续几次改动各自都没超预算,但累计相对 `$MB` 已经超了。需要单独回去看这 9 行现在具体是什么、要不要拆成多条独立登记,而不是简单地把预算数字改大——本文档只负责标出它,不负责裁决。

## 不算债务:已经在白名单里、按预算走的 T1 缝(2 个)

对照用,证明"上游文件改动"不是天然违规——只要走 `upstream-touch-allowlist.yaml` 登记 + 预算,就是纪律允许的:

- `backend/testing/desktop_beta_admission/run.sh`(+1/1,`forbidden_exceptions` 破例 + 白名单预算内——见 `00-upstream-touch-policy.md` §2.1)
- `desktop/macos/docs/desktop-updates.mdx`(+1/1)

## 已消化(归档)

目前还没有——所有 37+1 条都还在上面的"未消化"表里。第一条从这里挪走的时候,格式是:`文件 | 原因 | 用的哪种手法消化的 | 消化于哪次同步（日期 + 提交/PR）`。

## 用法

- **每次同步前**:重新跑一遍生成命令,新出现的文件说明这次同步又带来了新的分歧,加进对应分组(没有合适分组就新开一组);消失的文件说明要么已经处置、要么这次巧合没有触发核算——去查是不是真的处置了,处置了就把状态改成`已消化`并把这一行移到本文件末尾的归档区,不要直接删掉(删掉会让"复发不是无中生有"这件事无法验证);当次同步的整体结果仍按老规矩写进 `sync-log.md`。
- **每次同步后**:PR 描述里贴处置了哪些条目(参考 `templates/sync-pr-body.md`),更新本文件对应行的状态。
- 状态从`待处置`/`待诊断`变成`已消化`的判定标准是:再跑一次生成命令,该文件不再出现在 `violations` 里(要么因为回退成了上游字节,要么因为改用了 `backend/fork/` + 导入期补丁这类不触碰上游文件的手法)。
