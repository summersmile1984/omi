# CF-4 录音存储与真实本地 target runner

本包从整合基准 `ff3610160dff4c27dba3c1031d4982451efaa2d9` 的短期工作树推进，前置包括固定 native workerd 入口、Tasks422（原提交 `dd91fcc54c5e69f258cdb94f32a0f8293eaacf3a`）、共同 HTTP cases/Server runner `0227847d288bcc0913435b084d52ad73dd583310`，及独立旧音频 await 修复 `9ae383cb4b8634131e51650a540b0acdc978e506`。本包没有长期 target 分支、上游文件/锁文件修改、外部资源创建、部署或发布。

## 已实现的业务 owner

| 表面 | 权威与本包变化 | 已证明/边界 |
|---|---|---|
| `/v4/listen` | HTTP Upgrade 的 JWT/签名内部身份，原生 DO 连接 | 实际 PCM→ASR→D1→detail；保留现有原生协议 |
| `/v4/web/listen` | 每连接独立 DO bootstrap，首帧 JWT 向 Auth 查询真实会话 | 实际认证后录音；两个 Web DO 的写入竞争由 D1 token 决定 |
| conversation/session 绑定 | `recording-store.ts`，新 `cf_live_recording_sessions` 的 `(uid, recording_session_id)`，初始 token 与当前连接 token | 先建立 D1 owner，再发 `ready`/`conversation_session`；旧连接不能写新 owner |
| 转录持久化 | 同 UID/conversation/current token 的 `UPDATE … RETURNING id` | 显示前提交；不以包含 FTS trigger 写入的 `meta.changes` 判断单行成功；重连在已存最大时间后接续 |
| 完成/删除旧录音 | 元数据保留原 ID 归属；`in_progress` 才可恢复 | completed/deleted 生成新 ID，旧映射不能重建被删除 conversation |
| 同 DO 晚完成 open | 不随 close 清空的 `recordingAdmissionChain` | 上次 D1 open 完成后才开始下次，阻止晚完成旧请求反过来拥有新录音 |
| 账户删除 | 新迁移的 UID fence triggers，Jobs purge/residual 清单 | 既有删除围栏同时阻止新 capture 和迟到转录；不声称重新验收所有外部删除 provider |
| 显式 finalize | 既有 `/v1/conversations/:id/finalize`→Queue→Jobs→Core | 本包不复制另一最终化状态机；实际队列完成后读取 completed、派生 memory/task |

转录归并按 speaker/start/end 去重并允许修正，校验有限时间与正区间；2000 段、总 500000 字符、单段 100000 字符为明确容量上限，超限关闭录音并保留已提交内容，不静默截断。语音质量及长时录音仍需另验。

## 可重复执行的本地/CI 入口

```sh
npm ci --prefix deploy/cloudflare
bash deploy/cloudflare/ci/product.sh
```

同一 `fork-cloudflare-product-core` 在 `.github/checks-manifest.fork.yaml` 的 local/ci 两 lane 运行。`contracts/deployment/**` 变化同时选中 Server 和 Cloudflare runner。脚本生成新的私有输出目录，构建真实七个应用 Worker，加仅提供结构化推理的第八个测试 Worker与独立 ASR WebSocket server；执行全部正常 Auth/App migrations，启动固定 Wrangler/workerd，再跑共同 core 和独立 recording cases，最后清理所拥有的进程组。它没有 Core stub，也不更改应用鉴权/存储/队列 handler。

`metadata.json` 的五字段为 `api_origin/auth_origin/target/brand_id/trace_dir`。`fixture.json` 留下 revision、实际 worktree status、npm/Python lock 哈希、工具版本、migration 文件及编译模块/配置哈希。原始日志、正常迁移后的本地状态与报告保留在输出目录中；secrets 为单次生成且只在该目录。重跑要求新的目录，不接管已存在目录或 broken symlink。

- 固定入口仍是 `scripts/python-worker.mjs`：workers-py1.16.7、uv0.12.3，Wrangler4.127.0、workerd1.20260826.1；无锁升级。
- `LocalProcesses` 对每个实际进程组提供五分钟 deadline 和取消；取消先关闭后续阶段准入，结束父子进程；启动失败、非零状态不打印通过。Ready 必须来自本次 Wrangler 日志并通过本次端口的健康请求。
- interactive fixture 最长一小时。新目录保留日志/状态；不会猜测删除其他目录或远端资源。
- Auth 是正常受限服务。同 loopback 四次注册可能触发已有 429；recording driver 只按真实 `X-Retry-After` 等待一次（最多60秒），保留该429记录，未关闭任何鉴权限流。
- Python fresh 下载/TLS、平台缓存依然是真实前置条件。正式入口的失败不会换成轻量业务替身。

## 证据

工作日志根目录：`/tmp/memweft-implementation/cloudflare/product/`。

| 命令/证据 | 结果 | 覆盖 |
|---|---|---|
| `npx vitest run tests/realtime.test.ts tests/recording-store.test.ts tests/local-target.test.mjs`，`admission-after.log` | 40 tests通过 | 实际 SQL 全 migration、生产 session 的延迟 quota/Blob/open、提交前广播拒绝、进程树取消/期限及资源归属 |
| `admission-before.log` | 1失败 | 旧 open 未结束时第二 open 已执行；新增串行 owner 前的直接复现 |
| `session-admission/before-final.log`、`after-final.log` | 旧代码2失败→25通过 | 独立窄提交：quota/Blob等待后旧音频不能进入新连接 |
| `formal-7/logs/core.log` 与 `formal-7/trace/core-results.json` | 8/8 | 原样共同公开HTTP身份/恢复/引导/Tasks/跨UID/422/撤销 |
| `formal-7/logs/recording.log` 与 `formal-7/trace/recording-results.json` | 6/6 | Native Bearer与Web首帧JWT、D1持久化、重连隔离、实际Queue最终化、派生memory/task、终态滚动新ID、撤销 |
| `formal-7/logs/*-compile.log` | 8次真实dry-run | 七个应用工件及仅推理测试 Worker；这些就是本次本地运行的 frozen modules |

`formal-7` 是新增 open 串行防护前的实际业务闭环证据；最终 staged candidate 的重跑命令、结果和工件归属将在同目录的最终 gate 日志记录，不能把先前工件当作最终字节验收。

保留了故障过程：首次离线工具缓存缺失、Wrangler TestHarness 无法代理 provider WebSocket、早期 native 控制 fd 问题、D1 FTS changes 误判、fixture cleanup重复kill、注册429、native probe未带Upgrade JWT。它们分别推动正式入口/断言/fixture改正，未通过隐藏失败放宽产品契约。部分失败运行的编译目录与本地状态已在进程退出后清理，日志与报告仍留存。旧端口33062给原生/Electron终端使用，本runner使用独立动态端口，数据不共享。

## 未完成，不能视作发布资格

- 清单仍有36个明确归属的 blocked 注册路由，本包没有改成 unsupported 或删除检查以减少数字。
- 自动静音/断线最终化、持续录音中主动 finalize 后自动轮换及完整生命周期通知尚未接齐。
- 完整聊天/SSE、语义检索、上传/导出及删除全链路，provider失败后的全部副作用重试和重复投递语义需后续 CF-4 用同一实际runner扩展。
- Vectorize/Images没有本地真实替代证明；受控推理不证明模型质量、成本、远端配额或完整纯CF产品能力。
- 当前shared core固定8项，recording为独立CF切片，尚不构成两target完整recording/error wire parity，也不构成所有白牌终端验收。
- CF5 complete-product、dual-target、prior-schema三个 qualifier仍按真实缺口pending。所有本地报告明确 `release_qualified:false`；无生产部署、远端migration、旧版本兼容或客户数据声明。
