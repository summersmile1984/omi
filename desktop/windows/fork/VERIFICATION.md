# CLIENT-1/WL-4 本地验收记录

日期：2026-09-04。基线：`44327be4585a`（root 已集成 Android 的候选）。
实现树：`/Users/macstudio/Documents/memweft-worktrees/implement-electron-identity`。
原始日志及合成工件：`/tmp/memweft-implementation/electron/`。
本地提交，不 push、不发布、不操作生产 Omi。

## 已验证的范围

| 范围                     | 结果与限制                                                                                                                                                                                    |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 上游模式                 | `pnpm test`：561 test files 通过/6 跳过，5558 tests 通过/33 跳过。上游 tests、源码、lock、package 均未编辑。                                                                                  |
| fork 核心                | 12 tests：真实 IdentityClient/IdentityOwner、safeStorage 边界、原生 HTTP 临时服务、profile CORS owner。覆盖成功与主要错误/竞态路径。                                                          |
| staging                  | 3 tests：两个真实 target、hash 漂移/不安全输出拒绝、旧 upstream brand 拒绝。检查 lock 与上游测试逐字不变；源码字符串断言仅标作静态闭包检查。                                                  |
| 每个 target 的真实消费者 | 5 tests：main IPC 冷恢复与公开接口、renderer owner/姓名/注销失败、原生 header WS、实际登录表单错误。两 target 共执行 10 次，并非 10 个不同业务场景。                                          |
| 完整构建                 | 每个 fresh stage 执行 `pnpm build`：main/web TSC、Electron main/preload bytecode 验证、renderer 与 PiMono extension 均成功。无安装包/签名/发布。                                              |
| Server 实际运行          | Node/PG Auth34811 + API34810；Electron UI 注册、JWT 刷新、renderer Tasks 创建与完成200、关闭进程后同用户及 completed task 恢复、UI 注销完成清理、旧 JWT 访问受保护 Tasks401、错密码 UI 提示。 |
| Cloudflare 实际运行      | 真实 workerd Auth33058 + Edge33062/Core33064，完成同一身份/Tasks/冷恢复/注销闭环。                                                                                                            |
| 操作系统范围             | 实际运行均为当前 macOS 主机上的锁定 Electron39.8.10，使用两个不同 `.forktest.` userData namespace。不能视为 Windows DPAPI、Linux libsecret/kwallet 或 installer 实测。                        |

两个 UI 流程都停留在真实引导页。没有完成 onboarding、打开 Tasks 产品页面、
授权麦克风或扫描文件。Tasks 是真实 renderer 中使用该公开 JWT 接口发出的 API 调用，
不是服务假响应；不将此证据扩大为完整桌面产品验收。

## 可重现命令与日志

工具链：Node22.23.2、pnpm10.27.0、lock 中 Electron39.8.10。
`make setup` exit0；`pnpm install --frozen-lockfile` exit0（原始目录）。

```bash
# 正式 fork 行为与两个完整构建；不连接外部服务
PATH=<Node22>/bin:$PATH bash desktop/windows/fork/test.sh
# 独立上游模式
pnpm --dir desktop/windows test
# 显式基线和真实 pending diff，而非 HEAD=base 的零文件自检
python3 .github/scripts/run_checks.py --lane local --base 44327be458 \
  --pr-body-file /tmp/memweft-implementation/electron/pr-body.md
# 当前机器上执行 Linux 所选的通用 fork 检查，不冒充 Linux OS 运行资格
python3 .github/scripts/run_checks.py --manifest .github/checks-manifest.fork.yaml \
  --lane local --base 44327be458 --platform linux
```

- `upstream-suite.log`：独立上游 suite exit0。
- `formal-tests-4.log`：12 core +3 prepare +每 target5 staged tests，两份完整 build exit0。
- `upstream-exact-pending-3.log`：36 文件的实际 pending 范围，17项上游 manifest checks 全通过。
- `fork-linux-pending.log`：本次 workflow 选择 Electron、Flutter、CI diff-base、upstream-touch 四项；四项全通过（Electron与Flutter均实际执行），exit0。
- `server-build-3.log` / `server-stage-3/`：实际 Server UI 使用的完整工件。
- `server-auth-flow-4.log`、`server-auth-4-{signed-in,restored,signed-out}.json/png`：真实 Server 证据。
- `cf-accepted-build-1.log` / `cf-accepted-1/`、`cf-auth-flow-5.log`、`cf-auth-5-*.json/png`：真实 Cloudflare 证据。
- `cf-final/` 的 manifest 额外记录每个 fork 源文件 hash，便于 pre-commit 工件溯源。

UI 验证后删除了从未被调用的 `signOut(full=false)` 入口，公开 signOut 现在必定完整清理；
原两次 UI 路径使用 full=true，与当前行为一致。随后重新执行正式双 target 测试与构建。
当前 source stage 也补录自身源码 hashes；这些不更改服务端认证合同。
最终 `cf-final-auth.log` 再次使用新工件实际通过相同完整身份/Tasks流程（API200、旧JWT401）。

本次 fork workflow 新增 Electron 条件安装；该文件同时触发已有 Flutter/macOS 检查。
Flutter 按3.44.5离线 frozen bootstrap，生成的上游 ephemeral Package.swift 已定点恢复。
macOS完整 compile 本轮未重跑：当时仅5.9GiB可用空间，
低于 root 协调的25GiB完整编译门限；未用平台过滤冒充完整 fork manifest 通过。
root 在既有 macOS CI 形态上已有独立完整编译证据，新增 Electron 调度仍由本包检查。
轻量 macOS identity check另行按本机平台执行：12 Swift tests +4 stage tests通过，
见 `fork-macos-identity.log`。因此本轮fork选中6项中5项有实际通过结果，完整macOS compile一项未跑。

## 失败证据与修复归因

1. 初版 wrapper 使用动态 `require('./application')`，Rollup 未纳入 application，
   上游 bytecode 验证拒绝896字节空壳。改为静态 import；之后完整构建通过。
2. 初版登录表单在已有 canvas 伪元素下面，DOM 存在但实际看不见。修正层级并把
   fork renderer 纳入 Tailwind 扫描；`cf-login-visible-launch.png` 及 accepted 截图核实。
3. Node web fetch 自动携带 Fetch Metadata，却没有浏览器 Origin，CF Auth 返回403
   `MISSING_OR_NULL_ORIGIN`。`cf-auth-flow-3.log` 保留真实失败；改原生 HTTP(S)，
   未伪造 Origin 或改变服务器 CSRF。`http.test.ts` 执行实际临时 HTTP 请求。
4. 冷恢复时空白 companion window URL 抛异常，主 IPC snapshot 返回 unavailable。
   `cf-snapshot-1-identity.json` 保留失败；`runtime.test.ts` 以实际生产 register/owner
   覆盖尚无 URL 的窗口，未知窗口不获得身份事件。
5. Server fixture loopback 丢弃合法 chunked body，导致400空字段校验。
   此为专用测试代理缺陷，非 Better Auth 生产桥或 CSRF 缺陷；root 独立修代理。
   客户端已缓冲 JSON，改标准 `request.end(body)` 发送正确 Content-Length。
6. `/v2/apps` 对 Server 公开，注销后200不是撤销缺陷，也不能作撤销证据。
   最终脚本使用受保护 `/v1/action-items`，旧 JWT 明确401。早期误命名
   `cf-auth-signed-in.*` 和 `server-auth-flow-3` 不计入最终通过证据。
7. 正式检查初跑暴露子进程拾取系统 Python3.9；明确将 venv Python3.11 放入 PATH 后
   通过。Flutter bootstrap 产生的 ephemeral 文件造成一次 diff-hygiene 失败，恢复该
   单一生成物后重跑通过；未使用 formatter hatch 或更改上游文件。

Server专属 runner PID7786 已正常 SIGTERM；项目 `memweft-contract-765ec7d4dc83`
全部容器清理，未触碰其他项目。所有 Electron 测试进程均由自己的 Playwright实例关闭。
合成账户凭据只留私有临时文件，不进入源码、日志正文或前端配置。

## 尚未验收

Windows/Linux 原生 installer、OS安全存储/无keyring UI、macOS本机以外的实际UI；
正式签名/更新feed、OAuth、推送、蓝牙/固件、全视觉品牌/AI人格/外链替换；
完整 onboarding、真实音频硬件与付费模型、Tasks产品页面。
WS 当前证据为生产 handler 的受控 transport 行为回归，未把它写成此包真实音频E2E。

## 实际请求头回归（2026-09-04 后续包）

基线 `d0dcd36ae8`。初次真实引导的生产 Axios 请求被 native CORS owner 拒绝：
`cf-onboarding-tasks-renderer.log` 记录 `X-App-Platform` 不在 Allow-Headers。
原身份包的简化 fetch probe 没有携带平台/版本/设备请求头，不能覆盖该调用者。

修复沿用唯一 `native/apiAccess.ts`，加入生产 Axios 的三个身份请求头，并从共享
`BYOK_HEADER_NAMES` 派生已有 BYOK 名称。未知 preflight header 不获得 native grant；
精确 renderer/API origin 与 Auth 路径排除保持。`staged/apiAccess.test.ts` 实际执行
生产 Axios interceptor，从结果构造 preflight 并交给真实 native adapter；不是手抄
请求头断言。既有 native 错源、换窗口和 Auth 路径反例继续执行。

实际 `onboarding-fixed-stage` 的 `/v1/users/language` 返回200，见
`cf-onboarding-tasks-final-http.log`；该工件同时带有后续图谱加载隔离修复，
这里只认可请求头行为，不以之宣称全平台发行资格。原始日志仍在
`/tmp/memweft-implementation/electron/`。
