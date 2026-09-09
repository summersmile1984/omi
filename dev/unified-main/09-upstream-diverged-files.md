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

## 现状快照（2026-09-05，候选 `3fa5ce41d4`）

候选 `codex/unified-delivery` 以 `upstream/main` 的 `c4880cd5f6` 为祖先，
`git rev-list --left-right --count upstream/main...HEAD` 为 `0 263`。

```bash
python3 scripts/fork/check-upstream-touch.py \
  --base upstream/main --head HEAD --upstream-ref upstream/main --json
```

结果是 **0 个 violations**。`allowed` 仅有两个预算内接缝：

- `app/lib/flavors.dart`（+3/+3）
- `desktop/macos/docs/desktop-updates.mdx`（+1/+1）

`upstream_sync_plan.py --base HEAD --upstream upstream/main` 也报告无合并
冲突。这是本地候选的同步和源码边界证据，不代表它已推送、合并或发布。

## 已消化（2026-09-05 收敛）

| 原分歧 | 最终所有者 | 验证 |
| --- | --- | --- |
| `backend/config/prerecorded_stt.py`、`stt_provider_policy.py`、`utils/stt/{pre_recorded,streaming}.py`、其 upstream 测试 | 还原上游字节；`backend/fork/patches/speech.py` 在 self-host profile 下替换真实选择器、provider 与 `ListenReceiver` 类 | 67 个 upstream/fork speech、transport 与 seam 测试通过 |
| `backend/utils/llm/providers.py` 与 cloud-neutral routing 测试 | 还原上游 provider 表；fork 的 `local_llm` 工厂和 registry 继续拥有自托管路由 | 38 个 local-LLM/model/seam 测试通过 |
| `backend/{requirements.txt,pylock*.toml}`、Modal/Pusher Dockerfiles、Pusher source-closure 测试 | 还原上游运行时和辅助镜像；仅 `deploy/self-host/Dockerfile` 安装 hash-pinned `backend/requirements-fork.txt` | 38 个 self-host/Pusher 测试、配置自检和离线 Docker import 验证通过 |
| `desktop/macos/Desktop/Sources/AuthService.swift` 及 DEBUG bearer-token 测试 | 还原上游 AuthService；fork staging overlay 继续替换原生 Better Auth owners，并将当前 `getIdToken` 摘要写入 `source-owners.json` | `desktop/macos/fork/test.sh` 通过：12 个身份测试、stage/asset 合同与双 target stage |
| `docs/api-reference/app-client-openapi.json` | 还原上游生成快照；fork Kokoro transport 不改上游 app-client contract | `export_openapi.py --surface app-client --check` 和 43 个 OpenAPI 合同测试通过 |
| 试验性 MiMo/MOSS selector 测试 | 不再向上游 selector 注册未获 profile 接纳的 provider；保留隔离的 operator adapter 及其显式端点/egress 测试 | 42 个 adapter/config 测试通过 |

## 不算债务：允许的 T1 接缝（2 个）

这些上游文件由 allowlist 明确登记，并在当前候选的累计比较中没有超预算：

- `app/lib/flavors.dart`
- `desktop/macos/docs/desktop-updates.mdx`

## 用法

- **每次同步前**:重新跑一遍生成命令,新出现的文件说明这次同步又带来了新的分歧,加进对应分组(没有合适分组就新开一组);消失的文件说明要么已经处置、要么这次巧合没有触发核算——去查是不是真的处置了,处置了就把状态改成`已消化`并把这一行移到本文件末尾的归档区,不要直接删掉(删掉会让"复发不是无中生有"这件事无法验证);当次同步的整体结果仍按老规矩写进 `sync-log.md`。
- **每次同步后**:PR 描述里贴处置了哪些条目(参考 `templates/sync-pr-body.md`),更新本文件对应行的状态。
- 状态从`待处置`/`待诊断`变成`已消化`的判定标准是:再跑一次生成命令,该文件不再出现在 `violations` 里(要么因为回退成了上游字节,要么因为改用了 `backend/fork/` + 导入期补丁这类不触碰上游文件的手法)。
