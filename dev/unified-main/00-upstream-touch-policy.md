# 00 · 上游文件零改动策略（fork 纪律第一条）

> 原则：**能不改上游代码就不改。** 先复用配置、资源与现有 fork 入口；没有合理扩展口时，才保留单一职责、≤3 个新增行的配置接缝。白名单只记录实际使用的接缝，不预留未来权限。
> 不为账面零 diff 复制整个上游模块、替换整个业务方法或增加源码重写。上游 PR 尚未创建时必须标为提案，不能把标题冒充链接。历史诊断保留如下；当前准入清单以 §4 和 YAML 为准。

## 1. 诊断：`feature/cloud-neutral-shim` 为什么改了 653 个上游文件

| 类别 | 文件数 | 发生了什么 | 可避免？ | 本方案处置 |
|---|---|---|---|---|
| 上游测试 | **164**（`backend/tests` 86、`app/test`、macOS/Windows 测试） | 为让 shim 行为通过而改上游断言 | 是 | 上游测试在"上游模式"（无 shim 环境变量）原样运行；shim/profile 行为写在 fork 测试目录（见 §5） |
| l10n | **99**（49 ARB + 50 生成 `.dart`） | 加了自托管文案键后重新生成 | 生成文件：是；ARB：大部分 | 生成文件差异不提交、CI 生成；ARB 只保留自托管**必须**的新键并用 fork 前缀；品牌词不改 ARB（运行时委托或上游参数化 PR） |
| Windows 客户端围栏 | **~130** | 提交 `15cc5f19d0`（128 文件）、`dc2d74fc62`（76 文件）在每个调用点加 Firebase/Google/供应商出站围栏 | 是 | `vite.fork.config.ts`（extend 上游 vite 配置）用 `resolve.alias` 把 `firebase/*`、Google SDK 指向 `fork/packages/*-shim`；调用点零改动 |
| 后端 provider 内联 | `tts_provider.py` +345、`prerecorded_stt.py` +182、`routers/tts.py` +153、`storage.py` +199、`stt/streaming.py` +75、`cloud_tasks.py` +49、`stt_provider_policy.py` +34、`endpoints.py` +34、`main.py` +30、`_client.py` +26 | MiMo/MOSS/SenseVoice/TTS/MinIO/Redis 队列的实现与分发直接写进上游文件 | 是 | 实现迁入 `backend/fork/**`，由 `backend/fork/main.py` 入口的补丁注册表在导入时挂入；上游文件恢复原样 |
| macOS 围栏与登录 | 68 | 同 Windows，加 Better Auth 登录 | 部分 | Swift 无运行时补丁：生成文件（`Sources/Generated/`，上游已把该目录排除在格式化外）+ Info.plist 键 + 少数 T1 钩子 |
| context-for-claude | 22 | 姊妹应用的自托管适配 | 部分 | 同 macOS 做法 |
| 纯格式化 | 19 | 工具版本与上游不一致 | 是 | 禁止；工具版本钉住上游 |
| 其余（`backend/utils`、`routers`、`database`、`llm_gateway` 零散） | ~40 | 注入点与小修 | 是 | 按 §3 技术目录归 T0；真修 bug 直接提上游 |

同一时期 `codex/cloudflare-adaptation` 只改了 34 个上游文件，因为它把整个实现放在 `deploy/cloudflare/` 且不 import 上游后端——这就是目标形态。

## 2. 三级规则

| 级别 | 定义 | 例子 | 守卫 |
|---|---|---|---|
| **T0 零改动（默认）** | 上游文件一个字节不变；fork 行为来自新文件/别名/入口/补丁/生成物/环境变量 | `backend/fork/main.py`、`app/pubspec_overrides.yaml`、`vite.fork.config.ts`、`deploy/**`、`brand/**`、`*.fork.md`、`checks-manifest.fork.yaml`、`fork-*.yml` | `check-upstream-touch.py` 默认零 |
| **T1 白名单钩子** | 上游文件里只读取配置，不承接业务逻辑；精确文件、有限行数、明确退役条件 | 当前仅 `app/lib/flavors.dart`：一个 import 与两个标题返回值 | `upstream-touch-allowlist.yaml` 逐条限新增行数 |
| **T2 禁止** | 改上游测试、锁文件/依赖清单、生成文件、机器人写入文件、CI 工作流、`AGENTS.md` 正文、纯格式化、把业务实现内联进上游文件 | shim 分支的 164 个测试改动、`tts_provider.py` +345 | 同上，命中即失败 |

### 2.1 T2 的唯一开口：`forbidden_exceptions`

T2 原本是绝对的。M1 撞到一个它没预见的情形：**上游自身的缺陷，落在 T2 区里，而 fork 侧不存在任何合法修法**——`backend/testing/desktop_beta_admission/run.sh` 的依赖集缺 `fastapi`，修它得改 `backend/**`，绕开它得改 `.github/checks-manifest.yaml` 或 `.github/workflows/**`，三处都是 T2（诊断见 `05-ci-matrix.md` §7.1）。

处理办法不是放松 `backend/**` 那条模式，而是给 T2 开一个**必须两处同时登记**的窄口：

- `upstream-touch-allowlist.yaml` 新增 `forbidden_exceptions:`，**只接受精确路径**；写通配符会被解析器直接拒绝（退出码 2），因为一个模式就能把整类禁令悄悄打开。
- 例外只豁免"绝不修改"这一条。该文件**仍然必须**在 `allow:` 里有自己的条目和 `max_added_lines` 预算，超预算照样红。
- 守卫在每次通过时把该路径标成 `[forbidden_exceptions]` 打印出来——一条只靠"检查是绿的"来体现的豁免，等于没人再复审它。

入选门槛（三条同时成立才允许）：**(1)** 被改的是上游自身的缺陷，不是 fork 的需求；**(2)** fork 侧确实无合法修法，且已把不可行的替代方案写清楚；**(3)** 同一个 PR 里已把修复排进 `upstream-prs.md`，上游接受后立刻删除例外与改动。想让 CI 变绿、想省事、fork 自己的功能需求，都不构成理由。

## 3. fork 功能的实际所有者

| 领域 | 源码与入口 | 边界 |
|---|---|---|
| 品牌 | `brand/<id>/manifest.yaml`、`scripts/brand/` | `flavors.brand.dart` 为消费端生成物；品牌条件不进入 `F.title` |
| Flutter 身份 | `app/fork/identity/`、`app/fork/prepare.py` | staging 输出仍为 `lib/fork/identity/`，消费者统一使用同一个 package URI；测试模板在 `app/fork/tests/` |
| 后端运行时 | `backend/fork/`、`backend/firestore_pg/` | 显式 `fork.main:app` 入口；只使用已有窄接缝，不复制上游业务模块 |
| 本地开发 | `dev/local.sh`、`dev/selfhost-local.sh`、`dev/docker-compose.dev.yml` | 统一生命周期与 profile renderer；不另建 fork 版上游 harness |
| 测试依赖 | `dev/requirements-test.*`、`scripts/fork/run-container-tests.py` | 独立 `.venv-fork-tests`；容器 fixtures 不进入上游 unit conftest；生产依赖仍归 `backend/requirements-fork.txt` |
| 桌面与 Web 构建 | `desktop/macos/fork/`、`desktop/windows/fork/`、`web/app/fork/`、`deploy/web/` | 当前已有 staged 构建入口，不为过去的 Swift/Next.js 接缝预留许可 |
| 部署 | `deploy/self-host/`、`deploy/cloudflare/`、`scripts/profiles/` | 普通 Linux 与 Cloudflare 是目录和 profile 维度；部署选择不等于 AI 供应商选择 |
| CI 与文档 | `scripts/fork/`、`checks-manifest.fork.yaml`、`fork-*.yml`、`AGENTS.fork.md` | 上游 workflow、manifest、AGENTS 和纯空白不修改 |

上游已有测试继续保留原来的运行条件。`listen_pusher_stack` 仍使用宿主 Redis；
不要为统一开发容器而复制整套测试，也不要用 Redis PING 代替 listen 业务场景。

## 4. T1 白名单（当前仅一个实际接缝）

| 文件 | 新增行上限 | 钩子内容 | 退役条件 |
|---|---|---|---|
| `app/lib/flavors.dart` | 3 | 一个 import；生产和开发标题读取 `kBrandDisplayName` | upstream 支持可配置标题后删除；`brand-configurable app title` 当前只是未提交提案 |

其余九个旧预留条目已移除，包括 macOS 更新文档：Cloudflare staging 桥接说明归
`deploy/cloudflare/release.md`。删除许可不删除现有 fork 功能。

后端、上游 CI/测试/锁文件、上游 AGENTS：**均为零条**。

## 5. 两条测试通道（上游测试永不修改）

| 通道 | 运行什么 | 环境 | 证明什么 |
|---|---|---|---|
| 上游模式 | `backend/test.sh`、`app/test.sh`、Swift/Windows 测试、`web-checks` 原样 | 不设任何 shim/profile 变量；不启用 `pubspec_overrides`/`vite.fork.config` | fork 没有改变上游行为（等价性） |
| fork 模式 | `backend/fork/tests/`、staged `app/fork/tests/`、`Desktop/Tests/Fork*`、`windows/src/**/*.fork.test.ts`、`contracts/` | 显式 self_hosted 或 cloudflare profile | fork 消费者与配置行为正确 |
| fork 容器资格 | `dev/tests/containers/` 与现有 PG shadow 场景 | 独立测试 venv、明确 Docker 前提、局部 conftest | 真实 Redis/PG 语义；不改变普通上游 unit 收集 |

shim 分支上那 164 个测试改动的等价断言，全部落到 fork 模式的测试目录；`backend-test-discovery` 清单检查保证它们被 runner 发现。

## 6. 守卫与度量

- `scripts/fork/check-upstream-touch.py --aggregate --allowlist dev/unified-main/upstream-touch-allowlist.yaml`：对**完整分歧**（`merge-base(HEAD, upstream/main)` 起）中存在于 `upstream/main` 树的每个文件——不在白名单 → 失败；在白名单但超行数 → 失败；命中 T2 类别 → 失败，并输出对应的 T0 做法提示。`upstream/main` 缺失时 exit 2（无法评估不等于通过）。进 `checks-manifest.fork.yaml`（`fork-upstream-touch`，`triggers: all`）。
- 差异数量使用同一个 aggregate guard 的结果，不用提交历史中的改动路径数替代最终 blob 差异。当前目标为一个实际接缝、零违规。
- `fork-diff-hygiene`：上游路径与已合入 upstream 祖先比较，fork 路径与事件基线比较；恢复上游字节不触发格式化循环，所有变更文本仍检查冲突标记。真实 Git 回归覆盖 `a41b07f2d1` 导入空行后被 `2ed93dbf52` 再次修改的实例。
- 上游 PR 队列记录在 `dev/unified-main/upstream-prs.md`：每接受一个，删一条白名单。


## 7. 首次实战校正（2026-09-03，S0 同步）

第一次按本策略执行同步时暴露的四条，已回写进上面的规则：

1. **上游把受预算约束的文件维护在天花板上。** `app/AGENTS.md` 11288/11500 字节、`backend/AGENTS.md` 38997/39000、`backend/utils/other` 12/12 个源文件。fork 只要加一行或一个文件就会把上游的守卫压垮，而且是**延迟引爆**：加的时候还有余量，上游长满后才炸。推论：fork 文件绝不能放进受阈值约束的上游包，指针也不能加进 AGENTS.md。本次据此把 `storage_minio.py` 移入新建的 `backend/fork/`。
2. **`git rerere` 会静默套用旧解法。** 本仓库 `rerere.enabled=true` 且有 152 条缓存，本次 13 个冲突里有 6 个被自动"解决"且**不留冲突标记**——`grep '<<<<<<<'` 查不出来。同步流程必须以 `git status` 的 `UU` 为准，并逐个复核 rerere 的结果是否符合当期策略。
3. **同步 PR 会稳定触发 5 项与"改动量"挂钩的检查**，与代码质量无关，属于流程样板（见 `06-upstream-sync.md` §7）。
4. **`desktop-e2e-flow-coverage` 无豁免机制**，上游新增 Swift 文件若自带覆盖缺口，同步 PR 就会红；不得为了变绿而编造 e2e 流程。

## 8. M1 实战校正（2026-09-03，落地自托管目标）

把 shim 分支的后端逻辑从上游文件里抽出来时，暴露出三条抽取本身自带的失败模式。它们都不是"改上游"的问题，而是"停止改上游"之后新出现的问题——M2、S2~S6 会一再遇到，所以写进规则。

1. **抽走了实现，就把分发一起抽走了；设置会变成摆设。** 最典型的一例：fork 用 `SPEAKER_EMBEDDING_PROVIDER` 提供 `http` / `sherpa_onnx` / `disabled` 三种边界，shim 分支是在上游函数体里加分支实现的。抽出来之后，校验函数还在读这个变量、还会通过，但上游的执行路径从头到尾只有一条 HTTP 实现——运营者挂载了本地模型，音频照样发出机器，**不报错、不打日志、转写照常返回**。
   规则：**凡是从上游文件里抽出条件实现，必须同时确认那个条件的分发点归谁。** 分发点若留在上游，就用 S5 注册表补丁把它接回来（`backend/fork/patches/speaker_embedding.py` 是范例），并写一条"关掉补丁就必须失败"的测试。只抽实现不接分发，等于把一个功能降级成一个被读取但无人执行的环境变量。
2. **不要顺手改上游的配置变量名。** shim 分支把上游的 `HOSTED_SPEAKER_EMBEDDING_API_URL` 改名成 `SPEAKER_EMBEDDING_API_URL`——靠的正是编辑上游 `_get_api_url()`。零改动之后这个改名没了依托，fork 校验一个名字、上游读另一个名字，校验通过之后必然失败。仓库里 charts、`.env` 模板、parakeet、测试用的全是上游那个名字。**上游的配置面就是契约的一部分，和 API 一样不改。**
3. **抽取是"手工搬运"，类型检查是唯一能兜住它的东西。** 本轮抽取丢了：一个正则（被写成同名字符串常量，`.fullmatch` 会变 AttributeError）、一个 `urlsplit` import（写成了 `urlparse`）、`io` / `Path` / `Optional` 三个 import、两个模块级全局。这些全部由 `pyright` 在 push 前抓到，没有一个是测试抓到的——被丢掉的那条路径当时还没有调用者。
   规则：**抽取类改动必须跑 `pyright` 覆盖整个 `backend/fork/` 与相关 provider 目录**，不能只跑改动文件；抽取出来的模块若暂时没有调用者，要么当场用补丁接上（见第 1 条），要么明确删掉，不留"以后会用"的孤儿。
