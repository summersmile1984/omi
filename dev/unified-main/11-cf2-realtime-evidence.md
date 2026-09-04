# CF-2 录音实时鉴权交付证据（2026-09-04）

本包基于 AUTH-1/CORS 的 `a5b21e143b` 与 `a61955201a`（本 worktree
HEAD 对应 `a846e4cae0`），不改 Auth Worker。Web 构建包为 `b338d5528c`；
独立浏览器证明使用 CLIENT-1 `7263d03`、`18fc862`、`a2d53f3d00` 联合源码。
两个部署目标仍是同一个 main 候选的矩阵维度。

## 行为与所有权

- Web `/v4/web/listen` 首帧改为实际上游使用的 `auth.token` 产品 JWT。
  `session-authority.ts` 从 Edge 移为共用 adapter，调用 Auth 在线 session/user
  权威检查；没有另加只验签名、忽略撤销的弱化路径。
- 原生 `/v4/listen`、`/v2/voice-message/transcribe-stream` 保留当前
  macOS/Windows 的 Bearer upgrade 协议。Web 鉴权后复用 uid 准入限流、
  可选 migration fence、fair-use 检查，再启动 ASR。Realtime 的 Auth/Core
  service bindings 与部署顺序跟随真实 owner。
- 错帧、二进制首帧、过大首帧、无 JWT、旧 ticket、重复 auth、超时后才返回
  的验证，以及撤销后重连都有负例。重复 auth 不把 JWT 转发给 ASR。
  已被 Auth 接纳的 legacy Firebase principal 也有正例。
- 仅退休 fork 的 `/v1/realtime/web-ticket`。真实上游
  `POST /v2/realtime/session` 是 live-model 凭据契约，保留注册并返回明确
  能力关闭错误。原 STT ticket 无法完成该契约，因此纠正为 CF-4 blocked：
  612 注册仍完整，现为 576 staging-owned + 36 blocked，并未删除用户目标。
  Wire 来源和边界见 `contracts/realtime/web-listen.md`。

## 本地命令与结果

日志根目录 `/tmp/memweft-implementation/cloudflare/`。

| 命令 | exit / 证明 | 日志 |
| --- | --- | --- |
| CF 目录 `npm run typecheck`、`npm test` | 0；106 files / 792 tests | `cf2-typecheck-final.log`、`cf2-tests-final.log` |
| `UV_OFFLINE=1 OPENAPI_RUNNER_PYTHON=<repo>/backend/.venv/bin/python npm --prefix deploy/cloudflare run validate:backend-routes` | 0；上游 hermetic import 实际 612 注册一致。首次在线 uv sync 因 PyPI 超时失败；离线使用现有缓存，未改依赖 | `cf2-routes-final.log`、`cf2-routes-offline.log` |
| 五个 TS Worker `wrangler deploy --dry-run --config workers/<owner>/wrangler.jsonc`；两个 Python Worker `UV_OFFLINE=1 uvx uv==0.12.3 run pywrangler deploy --dry-run` | 七个 0；新服务依赖进入打包，无发布 | `cf2-dryrun-{edge,auth,realtime,jobs,rate-limit,api-core,api-ai}.log` |
| `wrangler d1 migrations list APP_DB --local --config <fixture>/realtime.json --persist-to <local-state>` | 0；CF-1 已应用 156 App migrations 的同一本地 D1 无待迁移 | `cf2-local/migrations-list.log` |
| `npm run verify:migrations` | 1；需要远端 token 才能列举 staging D1，本轮未提供 | `cf2-migrations-final.log` |
| `bun <fixture>/live-flow.ts` | 0；真实 Auth/D1 + Edge/Realtime/rate-limit 共九种 WS 场景 | `cf2-local/live-flow-final.log` |

真实 fixture：复制 checked-in Edge/Realtime/rate-limit 配置到临时目录，名称
改为 `memweft-local-cf2-*`，绑定对应本地服务，关闭账号迁移围栏、删除远端
AI binding，以 `ASR_WS_URL=http://127.0.0.1:33065/listen` 指向本地 Bun
合成 ASR。共享 assertion secret 从 root 的本地 Auth fixture 复制到
mode-0600 `.dev.vars`，不打印。App D1 沿用 CF-1 的本地状态目录。
命令仅 `wrangler dev --local`：Edge 33062、Realtime 33063、rate-limit 33066；
显式 inspector port 防止并行实例抢默认端口。Auth 是 root 的真实 D1 33058。

探针新建 synthetic 用户并取得 session/JWT（不打印）：Web 两次正常连接、
两条 native Bearer 连接都收到合成转录；错误首帧、坏 JWT、重复 auth 被关闭；
logout 后旧 JWT 的 Web 重连和 native upgrade 都被拒绝。后两项通过真实
Auth session 删除验证。单测仍用受控 Auth 响应 seam，不伪称其已做真实签名。
过期、issuer/audience、账户删除等密码/JWT 负例继续由 AUTH-1 套件负责。

独立 CLIENT-1 Chromium 使用真实 `useRecording → audioCapture →
transcriptionSocket → Edge → Realtime → Auth/D1`，输入为明确的合成媒体流，
ASR 是合成 provider。一次首帧 auth 后发送 399 二进制帧、1,089,270 bytes，
收到 399 合成转录，`/record` Live Transcript DOM 可见文本。客户端证据：
`/tmp/memweft-implementation/whitelabel/client1-cf2-browser-proof.log` 与
`client1-cf2-transcript.png`。这不是模型质量或对话/记忆持久化证明。

## 仍未证明

本轮 Python Core 在临时 project root 或原目录重启，均在 workerd 加载
Pyodide 时出现 TLS disconnect；应用没有启动，API 读取返回 503。
这与 CF-1 较早成功运行 Core 的证据并列记录，不能用旧证据替代当前联合
API 验收。日志为 `cf2-local/core{,-final,-packages,-project-root}.log`。
没有修改业务代码绕过该问题，也没有把录音鉴权成功说成所有 API 已通过。

CF-3/CF-5 仍需资源命名、发布器正式接线及工具/runtime 闭包；CF-4 仍需业务
缺口和 live-model owner；CI-1/INTEGRATION-1 仍需跨 target 同候选完整
API→录音→对话/记忆及白牌资产门禁。本轮无远端部署或生产验收声明。
