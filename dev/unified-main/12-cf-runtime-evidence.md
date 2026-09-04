# CF Python 运行入口与最小业务持久化验收（2026-09-04）

本包基于 `cad36631b5`（CF-2）继续。它交付统一的 Python Worker 工具入口，补上真实 Auth → Web → Edge → Python Core → D1 的任务读写证据。**未证明完整 CF 发布、所有业务路由、所有错误语义与 Server 对等，亦未创建/部署远端资源。** CF-3 品牌资源 manifest 和 CF-5 当前 Moonshine 发布器仍需后续实现。

## 故障、修复与边界

- 原正式命令 `uvx uv==0.12.3 run pywrangler dev` 从未固定的 dev dependency 选中了 workers-py 1.17.1；该版本原生最低 Wrangler 是 4.127.1，仓库 npm 锁为 4.127.0。deploy dry-run 不执行同一个 dev 版本 guard，旧 dry-run 通过不能抵消这个启动失败。
- 本机已安装的官方 workers-py 1.16.7 原生最低 Wrangler 为 4.109.0，且实际使用 uv 0.12.3 读取现有 `pylock.toml` 的 10 个 requirement、同步两种环境并启动成功。没有改包内 guard、上游文件、npm/pylock，亦没有改兼容日期。
- `scripts/python-worker.mjs` 统一本地、staging 和 production 现有发布器的所有 Python 调用：固定 uv 0.12.3 / workers-py 1.16.7，检查安装的 Wrangler/workerd 与 npm 锁相同，先 sync 并检查 Python 锁未变，随后才进入 dev/deploy。npx 禁止临时在线选择另一个 Wrangler。实际工具版本不符、依赖准备失败或锁漂移会停止；新行为由现有 `fork-cloudflare-routes` local/CI lane 运行的 Vitest 套件覆盖。
- 默认命令创建隔离工具环境；本机 registry 缓存对 pyjson5 的 wheel 记录无法完成 fresh tool resolution，在线下载也出现 TLS timeout。`UV_OFFLINE=1 npm run python -- api-core sync` 实测 exit 1。**本包没有宣称全新机器联网安装已验证。** 通过 `CLOUDFLARE_PYWRANGLER_EXECUTABLE` 使用已安装的 1.16.7 来完成真实运行，入口仍检查准确版本，uv 仍固定；不接受任意工具版本。
- 之前直接 workerd 启动也发生 Pyodide bundle 的 TLS 错误。诊断用其官方 cache 参数首次成功下载后，当前入口使用该缓存完成多次真实重启。首次成功时网络也可能已恢复，不能把 cache 参数宣称为 TLS 根因修复。缓存不关闭 TLS 或完整性检查；冷缓存仍需要可用的可信网络。
- 所选兼容日期实际加载 `pyodide_0.28.2_2025-01-16_15.capnp.bin`，约 17 MB。workerd 1.20260826.1 的[官方源码](https://github.com/cloudflare/workerd/blob/v1.20260826.1/src/workerd/server/pyodide.c++)对磁盘缓存和下载内容均执行编译内置 digest 校验；运行时选择见[Cloudflare Python 文档](https://developers.cloudflare.com/workers/languages/python/how-python-workers-work/)。工具宿主 Python 与部署 Pyodide Python 是不同层。

这是运行入口的共享原语，不是另外增加一个静态检查器。修复对应 2026-09-04 在已合入 `d1e05d64a1` 的正式本地命令上复现的实际故障；最近该入口历史只有该次 CF target 引入。已阅读 `FC-runtime-image-boundary`，其约束是部署镜像第一方/第三方模块闭包，本次是 CLI 工具兼容选择与缓存生命周期，使用 `Failure-Class: none`，不将临近类硬套在这里。

## 真实任务闭环

服务全部在本机，只有合成用户和任务：root Auth `33058`（真实 Better Auth+D1），Edge `33062`，Core `33064`，Web `33059`（白牌同事的真实 CF Web artifact）。App D1 复用已完成 156 个迁移的本地状态目录；未对远端迁移状态作新结论。

白牌同事使用原 Web Tasks UI：POST `/api/proxy/v1/action-items` → Edge/Core 200；整页 reload GET 200 且新任务可见；点击完成，PATCH `/v1/action-items/<id>/completed?completed=true` 返回 200、`completed:true/status:completed`；再次整页 reload 后任务不在 active 列表。任务 ID `2984eef1a166421fb37395797e8df0ae` 仅为本机合成 fixture。

本路以另一个真实 Auth 合成用户独立验证：列表不包含上述 ID，GET、PATCH、完成 PATCH 均 404，未更改对方任务。自有任务 POST/GET/完成 PATCH 均 200；无身份 POST 401，空描述 400。随后实际停止并用新 `npm run python` 入口重启 Core，同一 Auth session 换取 JWT 后读取原 ID 仍为 200 且 `completed:true`，最后本人 DELETE 204、GET 404。会话/JWT只保存在 `/tmp` 的 mode-0600 fixture 中，不输出到日志。

**CI-1 待对齐：** 相同 `POST /v1/action-items {}`，当前 CF 实测 400，Server 同事实测上游 FastAPI/Pydantic 422。上述 happy path、隔离和持久化通过不等于完整 wire contract 相同。任务向量 outbox/队列是实际本地绑定；没有真实模型或 Vectorize 搜索验收。CF-2 的真实浏览器录音采用合成 ASR，仍只证明录音协议与转录展示，不证明录音→持久对话→记忆→导出的完整产品链。

## 可复现命令与日志

日志根目录 `/tmp/memweft-implementation/cloudflare/`。命令 cwd 默认为 `deploy/cloudflare`。本次 `CLOUDFLARE_PYWRANGLER_EXECUTABLE` 为 `/Users/macstudio/Documents/memweft-worktrees/cloudflare-adaptation/deploy/cloudflare/python/api-core/.venv/bin/pywrangler`，只读使用其已安装工具；运行源码、配置和锁来自当前 implement-cloudflare，未切换或改动旧 worktree。

| 命令/行为 | 结果 | 日志 |
|---|---|---|
| `npm run typecheck` | exit 0 | `cf3-python-typecheck.log` |
| `npm test` | exit 0；107 文件 / 798 tests，其中新工具边界 6 tests | `cf3-python-tests.log` |
| Core：`UV_OFFLINE=1 uvx uv==0.12.3 run pytest -q` | exit 0；398 tests | `cf3-python-core-tests.log` |
| AI：同命令 | exit 0；118 tests | `cf3-python-ai-tests.log` |
| `UV_OFFLINE=1 CLOUDFLARE_PYWRANGLER_EXECUTABLE=... npm run python -- api-core deploy --dry-run` | exit 0 | `cf3-python-core-launcher-dryrun.log` |
| AI：同入口 `api-ai deploy --dry-run` | exit 0 | `cf3-python-ai-launcher-dryrun.log` |
| `node node_modules/wrangler/bin/wrangler.js deploy --dry-run --config workers/<name>/wrangler.jsonc`，name=auth/rate-limit/realtime/jobs/edge | 每个 exit 0 | `cf3-python-dryrun-<name>.log` |
| 默认隔离工具命令 `UV_OFFLINE=1 npm run python -- api-core sync` | exit 1，缓存不能完成 fresh resolution | `cf3-python-fresh-tool-offline.log` |
| 已安装 1.16.7 + pinned uv 的原生 `pywrangler sync/dev` | sync exit 0 / dev Ready+health200 | `cf2-local/pywrangler-compatible-sync-pinned-uv.log`、`cf2-local/core-pinned-pywrangler.log` |
| 新入口 `npm run python -- api-core dev --port 33064 --inspector-port 33074 --config /tmp/.../cf2-local/core.json --persist-to /tmp/.../local-state` | Ready + health 200，外部 fixture config 闭包有效 | `cf3-python-core-external-config.log`、`cf3-python-core-health.json` |
| 新入口实际启动/停止/重启 | 两次 Ready | `cf3-python-core-launcher-live.log`、`cf3-python-core-launcher-restart.log` |
| `bun /tmp/.../cf2-local/http-flow.ts` / 同命令 `--resume` | 两个 exit 0；隔离、本人CRUD、重启持久化 | `cf3-python-http-before-restart.log`、`cf3-python-http-after-restart.log` |
| `POST /v1/action-items {}` | CF 400；Server 422 对齐待做 | `cf3-python-empty-task.log` |

本地 fixture 准备步骤：复用 CF-2 的 `cf2-local/prepare.cjs` 生成只指向本机的配置；Core entry/main 为当前源码的绝对路径；在 fixture config 旁链接当前 `python_modules`、`pyproject.toml`、`pylock.toml`；`.dev.vars` 放本地 Auth 共用 internal assertion secret（mode 0600）。入口设置 `CLOUDFLARE_PYODIDE_CACHE_DIR=/tmp/.../runtime-cache`。Auth/Edge/Realtime/rate/ASR 的独立端口、合成身份流程见 [CF-2 证据](11-cf2-realtime-evidence.md)。不复制任何客户数据。此次 Core 已从 repo 内临时 config 移到 `/tmp`，repo 内 fixture config 与 `.dev.vars` 已清理。

初始网络故障原始证据保留于 `cf2-local/core-final.log`、`cf2-local/pywrangler-compatible-install.log`、`cf2-local/workers-py-tags.log`；已成功的 Pyodide cache restart 在 `cf2-local/core-cache-restart.log`。初次 isolation probe 曾误用 JSON body 向 query-only `/completed` 发请求，收到400；修正 fixture 按真实 query 契约后完成404/200断言，没有修改产品代码迎合测试。

白牌真实浏览器日志在 `/tmp/memweft-implementation/whitelabel/client1-cf-api-{created,reloaded,completed,completed-reload}.log`。这些是独立浏览器证据，不能把本路 Bun HTTP fixture 说成浏览器点击覆盖。

最终本包使用固定候选树执行 upstream/fork 两个现有 manifest（不是零 diff）：原始结果 `cf3-python-upstream-preflight.log`、`cf3-python-fork-preflight.log`，候选 SHA `cf3-python-candidate.txt`；Failure-Class 验证 `cf3-python-failure-class.log`。root 集成到统一候选后应再执行跨包 gate。本包没有修改历史审计结论去抹掉当时的失败证据。
