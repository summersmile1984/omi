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

## 尚未验收（首包时点，后续补证见下）

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

## 图谱加载与真实 Tasks 页补证（2026-09-04 后续包）

基线 `d0dcd36ae8`，工件 `onboarding-fixed-stage/desktop`。图谱字体 fetch 失败时，
React 的新 Onboarding state 已推进，提交却被 Canvas 的 Suspense 挡住；用户仍停在
语言页，持久化进度也停在1。`language-fiber-probe-run.log` 仅观察了这两个值；
未调用组件私有 setter、改路由或种入 onboarding 完成标记。

唯一变更是在 staging 的 Onboarding map import 接入 fork-owned `OnboardingGraph`，
以局部 Suspense 隔离这一个异步视觉子树。加载未完成时显示真实 loading 文案，
步骤正常提交；未伪造图谱就绪，也未修复外部字体下载本身。
`staged/onboarding.test.tsx` 执行真实 Onboarding/Language/progress owner，受控 seam
仅让 Graph 的字体 readiness 暂停：旧入口测试失败，新入口保存 step2 并继续，
解除 readiness 后图谱恢复。新测试由既有 fork runner 的 staged glob 自动发现。

实际 `onboarding-final-flow.log` exit0：新合成账户在真实界面注册，从姓名起走完
14步；监听开关关闭，开发包登录启动显示不可用；屏幕、麦克风、Automation、
快捷键/语音演示、外部 OAuth 和目标建议均走原 UI 的 Skip。Discovery 使用进程专属
USERPROFILE/ProgramData/APPDATA 目录，真实扫描只有1个合成文本文件。

随后点击首页 Tasks 卡片，在真实 Tasks 页面创建任务并完成，正常关闭应用后重启，
页面仍显示 completed；再通过同一当前身份向受保护 API 读取，状态200且
`completed:true`。最后 Settings → Account → Sign out，并等待 clearEpoch 增长。
证据：`cf-onboarding-tasks-final-{background,discovery,task-created,task-completed,task-restored,signed-out}.png`
以及 `{onboarded,restored,remote-persisted,finished}.json`。本轮没有启动 Server fixture，
因此此处新增真实产品页面证据只覆盖 Cloudflare；Server 保留首包的身份/renderer API 证据。

正式验证：`onboarding-upstream-suite.log` 为561文件/5558测试通过，6文件/33测试跳过；
`onboarding-formal-fork.log` 为12 core +3 prepare +每 target7 staged tests及两份完整 build通过。
`onboarding-before-test.log` 保留图谱回归的旧行为失败，`onboarding-after-test.log` 为新行为7测试通过。

边界：仍有旧 Omi 文案/图标/帮助链接，图谱外部字体未证明成功；未配置 Calendar 返回503、
实时模型返回409，不计入功能通过。macOS-host Electron 记录上游 Windows-only
`setTitleBarOverlay` 不可用异常，但进程正常继续；本轮不宣称 macOS 产品支持或
Windows/Linux 系统资格。完整录音/屏幕能力、外部模型、签名、安装、更新发行仍独立验收。

## WL-5：Electron 可见品牌投影（2026-09-04）

基线 `c465df4e02`，证据目录 `/tmp/memweft-implementation/electron/wl5/`。
真实旧工件 `onboarding-fixed-stage/desktop/out` 的 UTF-8 扫描有785个原始词典命中；
6页既有 runtime 导出中有6处 Omi（2文件）。这不是785处已确认的品牌泄漏：
代码中 `window.omi`、IPC、数据库键必须保持，图片/bytecode等不属于文本扫描。

同一 `prepare.py` 现在从 brand/profile 单源投影 displayName/personaName、supportEmail、
legalEntity 和公开链接。webApp 使用 resolved target/stage web origin。静态 AST 目录
在19个 reviewed source owner 中渲染48条/51处，5条/6处协议/storage输入保持并单列；
Home feedback/community、About映射与托盘 disabled updater 入口另外在 staging owner
执行。`brand-coverage.json` 描述该范围，不能替代执行后的行为验证。新增同类文案
即使有人更新 source hash，未登记仍会失败；新测试执行生产 stage renderer 证明此点。
独立review补齐TemplateHead/Middle/Tail后，动态品牌片段也明确拒绝等待分类；现有
`--omi-mica=`动态CLI前缀单列保持。`dynamic-before.log`复现旧分类缺口，
`dynamic-text-guard-final.log`覆盖JSX text和三类动态片段。

行为回归执行实际生成后的组件：onboarding/name/consent、Hub ask/menu、托盘菜单与
About；产品名和persona不同，复杂名称 `Field <Guide> & "Co"` / `Robin & "R"` 正常渲染，
community空时隐藏，反馈调用既有 main openExternal owner，隐私/条款/文档等href
来自manifest，版本IPC失败显示 unavailable，disabled updater没有beta/install/check
控件。托盘 Open/Quit 与监听 toggle 仍调用原来的 action owner。原 graph suspension
回归仅改用manifest期望产品名，其实际进度/恢复断言保持。

实际工件 `harbor-stage/desktop` 用合成 `Harbor Desktop` / `Harbor Guide`，身份为
`invalid.example.harbor.forktest.cloudflare`。`harbor-full-ui-run.log` exit0：真实CF
Auth33058/Edge33062注册→14步引导→Home菜单→About/General/Privacy→完整退出。
引导中的监听关闭、屏幕/麦克风/自动化/语音/OAuth继续原Skip，Discovery仅扫描1个
专属合成文本。`harbor-full-ui-*.png/.txt` 和 `*-about-links.json` 保留可见证据；
实际support链接只核对href，未声称这些example.invalid站点已经部署。

先前 `harbor-ui-run.log` 在14步后因为测试脚本没有trim标题中的空白而停止；
`harbor-ui-final-run.log` 通过已有真实会话恢复并补Settings/退出；最终独立新账户的
`harbor-full-ui-run.log` 从首登开始完整通过。该脚本未写onboarding完成位或私有React状态。

执行后的 `build-after.json`：28个UTF-8文件、714个原始词典命中（10文件），40个
二进制/图片/字体文件未扫描。`runtime-after.json`：23个真实UI文本导出，只有1个
`Friend` 命中，来源是“How did you hear”里的普通朋友渠道；这些导出没有Omi命中。
未改品牌词典或添加宽泛豁免去消除数字。建前/建后范围不同，不能将这些数算为
全产品泄漏清零率；main bytecode、图标、Canvas图像和其他页面没有这项证明。

执行命令：Node22.23.2、pnpm10.27.0，依赖复用同一 frozen pnpm-lock 的安装目录；
Python使用本树 `backend/.venv/bin`（make setup安装）。

- `make setup`：`setup.log` exit0。
- `pnpm test`（原 desktop/windows）：`upstream-suite.log`，561文件/5558测试通过，
  6文件/33测试跳过；上游源码、测试、依赖和lock均未修改。
- `python3 fork/ci_build.py`：`matrix-second.log`，两target各10个staged tests及完整
  TypeScript/bytecode/renderer build通过；最初fixture缺身份mock的测试导入失败保留在
  `formal-first.log`，不是实际Auth回归。
- Harbor真实candidate另跑10个staged tests和 `pnpm build`，`harbor-build.log` exit0，
  包含最终NameStep persona消费。最后exact scoped fork门禁覆盖12core+4prepare+双target
  各10staged与完整build，由既有 `fork-electron-native-identity` local/ci条目发现。
- `pending-upstream-final.log` / `pending-fork-final.log` 使用已stage文件清单选择，
  分别12项、2项通过；上游历史failure-class ratchet因shallow clone明确SKIP，
  不称已验证90日历史。最终提交后以真实base/head重复exact范围，见
  `committed-upstream.log` / `committed-fork.log`。没有更改fork workflow或扩大Swift gate。
- guard最后补动态片段分类后，新fresh staging的834个实际产品TS/TSX/HTML文件与
  Harbor实测工件的source逐字相同（`harbor-final-source-equivalence.json`）。该对比
  排除tests/build tools/metadata，不冒称bytecode可重现或最终安装包一致。

仍待后续独立工程包：所有旧logo/icon/动画资产、其他页面和连接器可见文案与域名、
模型system prompt人格和provider能力策略。Windows/Linux真机/安装/安全存储、macOS
产品兼容、iOS、发行签名、更新、推送、硬件BLE/OTA密钥资格均未由本机Electron证明。
图谱字体仍可能进入真实loading fallback；本包不宣称它已恢复。

## WL-5：Electron 品牌资产（2026-09-04）

基线 `551a9e1dc3d23f084e9b3d35e46ebe5fde3db854`，证据目录
`/tmp/memweft-implementation/electron/assets/`。这是前一文本包明确保留的真实缺口：
manifest换名后原窗口/托盘PNG/ICO、Orb八点shader、LegacyHome头像及内联标记仍使用
上游素材。本包以 `assets.mjs` 作为已有prepare入口的唯一资产生成owner，不新增品牌
来源、上游修改或依赖/lock变化；所有fixture都在临时目录，正式品牌仍待真实输入。

覆盖：主窗口/capture/bar/toast图标与三平台builder配置；三个托盘状态；Login/About、
ConnectorBrandMark自己的omi项（其他第三方vendor保持）、思考指示器、LegacyHome
白底回复头像；Orb静态fallback与真实WebGL纹理。全部3个消费输入先通过路径、字节、
PNG格式/CRC/尺寸/alpha校验，再生成PNG/七帧ICO/64×64纹理。build manifest及独立
asset coverage带输入/输出hash；splash明确unconsumed。没有静默旧图标fallback。

实际验收与静态闭包分开：

- `bash desktop/windows/fork/test.sh`：`formal-final.log` exit0，14 core（含2个资产
  行为测试的多种输入）、4 prepare、两target各14 staged tests和完整build通过。
  两个target分别使用H/N不同素材，第二品牌保留复杂文字输入。PNG/ICO是真实编码/
  解码后像素断言；GL mock执行生产OrbRenderer构造/render/dispose，验证premultiplied
  字节、初始正向、原frame波形投影及上传失败释放资源。
- staged `legacy-assets.test.tsx` 执行实际LegacyHome，带合成assistant回复，证明白色
  既有裁切底消费dark logo；`assets.test.tsx`执行实际Connector/思考状态和BrandImage
  的一次重试/原中性fallback。合成回复只是组件测试，不伪装真实模型对话。
- 最初完整build在遗漏captureWindow等3个主进程图标消费者处失败，
  `formal-first.log`保留；已把所有同路径imports纳入原AST迁移，并为新增owners固定
  源hash。随后 `formal-second.log`两target完整通过。
- `harbor-ui-run-final.log` exit0：真实CF Auth33058/Edge33062、已有合成账户由登录
  UI进入Home/About/General，真实切换旧版Home并Back导航，恢复新Home后完整退出。
  `harbor-ui-{login,about,legacy-home,signed-out}.png` 已人工看图。旧Home实际Sidebar
  的WebGL标记已换为H；旧文案 `omi` / `Ask Omi…` 仍可见，另开后续文本提交，
  不把当前资产包声明为无泄漏。未写onboarding完成位/身份token或私有React状态。
- `webgl-proof.cjs` 导入实际staged OrbRenderer/computeOrbFrame，真实Electron39.8.10
  WebGL2画出6帧。`webgl-run-final.log` exit0，`webgl-proof.json`有每帧输入、原
  barMix/barCount、GL error=0、像素hash/alpha数量；5种不同rasters（idle与静默
  listening按合同相同），高音量历史占用更多像素。`webgl-six-states.png`显示H、
  thinking旋转、logo→wave转换和高低波形。初次看图发现90度初始偏转，修正角基准并
  增加生产renderer初始正向断言；不是仅替换WebGL失败时的fallback图片。
- 这些GL帧用受控时间/音量历史输入；不是麦克风/TTS或外部模型的语义验收。
  原OrbAnimator/可见性/采样/门控/context recovery不修改，纹理随原renderer资源生命周期。
  品牌图替代八点外形，原圆盘/八点变形效果不再承诺；没有复制第二个动画状态owner。

正式gate继续沿已有 `fork-electron-native-identity` 的local/ci选择执行；新增行为
测试由现有runner自动发现，不加一条游离的扫描脚本或workflow。`pr-preflight --suggest`
在未提交时显示0文件，单列为metadata建议；pending gate使用已stage文件列表，提交后
再用真实base/head。日志见 `pending-{upstream,fork}.log`、`committed-{upstream,fork}.log`。
Failure-Class为none：现有registry没有品牌资产输入/消费所有权类，本包行为guard直接
针对基线551a的真实遗漏；不拿不相关的更新feed类别作装饰。

限制：Windows/Linux真实系统图标/托盘/安装包、安全存储、macOS产品/Dock、签名/更新
分发、iOS/Flutter/固件素材均未由本机Electron证明。原Windows-only titlebar/audio
helper在macOS-host的已知错误仍见main log，进程继续并完成上述UI路径；不算资格。
旧文本、系统prompt、其他页面/vendor资产与字体不属于这次已知素材hash/组件覆盖；
未做全包每个二进制的视觉OCR，也未消除所有Omi协议/storage名字。

资产最终冻结工件为 `assets/accepted/desktop`，重新prepare和完整build后再次运行
`accepted-ui.log` / `accepted-webgl.log` 均exit0。实际Electron nativeImage成功解码
256/1024窗口PNG及3个32px托盘PNG（`harbor-ui-native-png-decode.json`）；这是本机
PNG消费证据，不冒充WindowsICO/托盘系统资格。`accepted-assets-scan.json` 在out与
resources共74个文件中，对9个已知退休上游栅格素材做精确SHA256比对，0个命中；
范围只是已知素材身份检查，不能当全包视觉扫描。原目录 `pnpm test` exit0：
`upstream-suite.log` 为561文件/5558测试通过、6文件/33测试跳过。

本包pending上游12项通过，其中90日历史failure-class ratchet因shallow明确SKIP，
不称历史审计通过；fork检查2项覆盖零upstream touch与完整Electron测试/构建矩阵。
