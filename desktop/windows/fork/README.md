# Electron 原生身份与本地品牌工件

本包属于 CLIENT-1/WL-4。Windows/Linux 共用当前 Electron 源码；这里先交付
可构建的双 target 身份消费与本地隔离包边界。正式安装、操作系统密钥环资格、
签名和更新分发分别验收，不能由 macOS 主机上的 Electron smoke 代替。

## 唯一所有者

- `native/owner.ts` 持有 opaque Better Auth session、用户与短期 JWT。身份意图有
  取消代次；账户提交、完整异步清理共享一个队列。晚到的登录、姓名、refresh
  不能发布已离开的账户。清理失败持久化 receipt，重启后必须完成清理才能接纳新账户。
- `native/store.ts` 使用 Electron safeStorage 加密，文件中绑定 application identity。
  缺少安全存储、损坏密文和未登录是不同状态。Linux `basic_text`、未知 backend
  不允许保存或读取凭据；无明文降级、不导入旧 Firebase profile。
- `native/client.ts` 从唯一 profile 的 auth origin 调用 email 登录/注册、get-session、
  token、update-user、sign-out。opaque 只认 `set-auth-token` 响应头；JWT 仅作
  客户端缓存准入，执行共同 60 秒 iat 容差，签名/issuer/audience/撤销由 API/WS 权威校验。
- `native/http.ts` 使用 Node 原生 HTTP(S)，12 秒请求期限覆盖响应体，最多 1 MiB，
  不跟重定向。完整 JSON 通过 `request.end(body)` 发送。不合成浏览器 Origin，
  不携带浏览器 cookie，也不放宽服务端 CSRF。
- `native/runtime.ts` 注册可信一方窗口的 IPC。主进程由同一身份投影 AI profile、
  PiMono、Rewind 与 control-plane owner；main 的刷新直接向身份服务取 JWT。
  renderer 无权推 opaque、owner 用户 ID、任意 API origin 或后台会话。
- `native/apiAccess.ts` 接管上游已有的 Electron API CORS 边界，仅作用于当前 profile
  API 与当前应用 renderer 的精确 origin，保留 webSecurity。Auth 路径不走此边界。
  源码中的上游 API/analytics 通配名单不进入本地工件。生产 Axios 的平台、版本、
  设备与共享 BYOK 请求头进入明确白名单；未知 preflight header 不获得原生放行。
- `renderer/identity.ts` 是所有原有身份调用者的公共 facade。保留同 owner 的 User
  对象身份，保护现有跨 await 请求边界；不重复触发仅由 JWT refresh 导致的登录回调。
  姓名仅在服务器确认后投影；API 401 重试也不能把 A 的请求改用 B 的身份重放。
- 原有 capture/window authority、PCM16/linear16、native Authorization header WS
  协议保持。listen start 只接受当前 main owner 实际签发过的 JWT，并再次取 main
  的当前 token；换 owner 会关闭旧窗口会话。OAuth/provider 接线不在本包。

## 可审查的 staging

`prepare.py` 只接受私有/合成 manifest 和全新、仓库外目录，通过现有 WL-1 loader 与
profile resolver 解析 `self_hosted.local` 或 `cloudflare.local`。不接受 `omi-upstream`
作白牌验收。applicationId 为 `<windows_app_id>.forktest.<target>`，同时绑定：

1. app name / userData 路径，在整个 application 模块加载前设置；
2. Windows AppUserModelId、package/product metadata、installer/executable 名；
3. safeStorage 密文内的 identity；
4. 永久禁用当前本地工件的 updater 与 publish，忽略上游 update feed。

`source-owners.json` 固定本包审过的上游输入 hash。`source-stage.mjs` 只在副本执行
明确 AST/唯一 span 替换，所有迁移/删除输入都必须有 owner；上游发生漂移直接失败。
所有构建脚本指向生成的 fork builder config，锁文件和依赖版本保持不变。
endpoint 既写入显式 `.env` 又作为 Vite 编译定义固定，宿主环境不能覆盖它。
`build-manifest.json` 保留 source commit、profile、owner hashes 与
`release_qualified: false`。生成物不提交到仓库。

WL-5 的 `brand-text.json` 是审阅过的完整 AST 文案目录；`brand-stage.mjs` 在同一
staging 入口渲染产品名/AI 人格名、引导、Home shell、General/Privacy 与托盘文案。
覆盖源中新出现的 Omi 文案未分类会失败；协议/storage 字符串保持且在
`fork/brand-coverage.json` 单列。该静态闭包检查与实际组件行为测试分开计算。
`prepare.py` 从唯一 brand manifest 投影 persona、support/legal 与链接；web app 使用
已选 target/stage 的 resolved web origin。About 的帮助/文档/条款/隐私等链接及
Home 的 feedback/community 使用这些字段，空 community 隐藏；未提供 referral
合同，不继承上游 affiliate。About/托盘不提供 disabled updater 的 beta/feed/install
入口。Name/Ask/goal 等 AI 表述使用 persona，产品/窗口/系统菜单使用 display name。

此 stage 仍不是完整视觉白牌包：旧图标、其他页面/连接器的剩余文案、系统提示词、
AI/provider UI 仍需后续品牌/能力包逐项生成和验收；扫描构建 UTF-8 与实际 UI 导出时
须单列二进制、图标、字体的未扫描范围，不能把上游品牌自检当成无泄漏验收。

## 命令与正式检查

使用 Node 22、pnpm 10 和现有 `pnpm-lock.yaml`：

```bash
cd desktop/windows
pnpm install --frozen-lockfile
bash fork/test.sh
```

`test.sh` 运行 production owner/HTTP/CORS/secure-store 行为测试、真实 prepare 的
双 target 和错误路径测试、每个 fresh stage 的真实 IPC/renderer/Login/WS 行为测试，
然后完整 `pnpm build`（main/web TSC、Electron main/preload bytecode、renderer、
PiMono extension）。原上游 suite 用原目录的 `pnpm test` 独立执行。

`.github/checks-manifest.fork.yaml` 的 `fork-electron-native-identity` 在 local/ci 两 lane
发现该检查。现有 Fork Checks workflow 仅选中此项时安装 pnpm/frozen Electron
依赖，不改上游 Desktop Windows CI；Linux CI 编译不等于 Windows/Linux UI 实测。
所有测试端口由进程临时分配，无外部服务；ci_build 的两个 profile origin 仅为编译输入，
不启动应用或连接这些地址。临时 stage 退出即回收，缓存不作为正确性前提。

本地 UI 工件：

```bash
python3 desktop/windows/fork/prepare.py --manifest /private/brand.json \
  --target self_hosted --output /tmp/new-electron-candidate
# 使用锁定依赖；可将同平台已 frozen install 的 node_modules 链入副本。
cd /tmp/new-electron-candidate/desktop
pnpm build
pnpm exec electron .
```

这里只允许 `.forktest.` 身份和本地目标。禁止启动/停止生产 Omi；不要给测试输入
附带真实 OAuth、发布、签名或硬件资格。详细实际证据见 `VERIFICATION.md`。

引导图谱由 `renderer/OnboardingGraph.tsx` 的局部 Suspense 边界承载；字体未就绪
只影响图谱加载，不阻塞步骤提交/持久化。它不宣称图谱已成功，也不绕过可选权限
或 onboarding。真实 Tasks 页面补证目前为 Cloudflare target，本机 Electron 运行。

## 依据

Electron 官方 [safeStorage](https://www.electronjs.org/docs/latest/api/safe-storage)
说明 Linux 没有系统 secret store 时可能使用 `basic_text`。本包显式拒绝它。
[webRequest](https://www.electronjs.org/docs/latest/api/web-request) 的 listener 为单一覆盖
所有者，且 webContents/frame 可缺失；本包替换原 owner，不叠加另一个拦截器。
共同缓存规则见 `contracts/auth/client-cache-admission.md`。
