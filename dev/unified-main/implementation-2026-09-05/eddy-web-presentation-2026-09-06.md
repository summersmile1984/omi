# Eddy Web：品牌展示与双目标产物验证

## 问题与修复边界

实际浏览器登录后，原构建的服务端 Eddy 标题被 Home 客户端更新覆盖成
`Omi - Your AI Companion`。侧栏仍显示上游 wordmark、下载域名和 Discord，
目标编辑器、聊天头像和帮助入口也没有完整消费品牌配置。

共享 Web builder 现在向两个目标投影同一组公开品牌字段；在隔离源码阶段，
对 26 个已审阅的展示文件进行语法树转换，并记录文件哈希。帮助页、页脚、
移动端提示及活动标记采用 fork overlay。图片继续来自原有 manifest raster
owner，活动标记保留暂停与减少动画偏好，移除已无消费者的上游 wordmark。
应用页装饰代码改用 `createApp`，紫色光晕改为中性色。

模型提示词、API 协议字段和动态业务数据不进入展示转换。包含 `Omi` 的用户
姓名、应用描述和目标标题不会被替换。帮助页使用品牌声明的文档、问题和邮件
入口；不再挂载上游客服 iframe。未配置 community 时隐藏对应链接。当前下载
文案明确标识 macOS，并使用所选目标 API 的下载路径。

本轮源代码均为 fork 自有文件，没有修改上游源文件、测试、锁文件或 CI。
新测试进入现有 `deploy/web/ci.sh` 和 `web/app/fork/test.sh`，没有新增孤立门禁。

## 验证证据

基线提交 `fffdec2d2e`，工作分支 `codex/unified-delivery`。使用 Bun 1.3.14、
Node 22、Web 锁定的 TypeScript/Vitest 和 Prettier 2.8.8。

- `web/app/node_modules/.bin/tsc --project deploy/web/tsconfig.json`：通过。
- `bun test deploy/web/build.test.ts`：7 passed / 59 assertions。新增验证执行
  JSX/模板转换后的代码，覆盖带引号和模板字符的品牌名、保留动态数据、声明
  链接投影、无效协议拒绝、移除旧 wordmark、上游模式保留原资源。
- `bash web/app/fork/test.sh`：Bun 14 passed；Vitest 5 files / 27 passed。
  使用生产 Sidebar、GoalComposer 等组件及实际阶段转换；提交带 `Omi` 的目标
  标题后，回调收到原值。帮助和页脚不含上游客服 iframe，链接来自声明配置。
- `bash web/app/test.sh`：原版类型检查、5 个 Moonshine smoke tests、
  75 files / 419 Vitest tests 通过，原版文件未改动。
- `bun deploy/web/build.ts --target cloudflare --stage local --manifest <private>
--output <fresh>` 与同样的 `self_hosted` 构建均通过生产源码类型检查及全部
  28 条路由构建。最终产物目录分别是私有证据根目录下的
  `web-presentation-cf-20260906-c`、`web-presentation-server-20260906-c`。
- 实际运行最终 Cloudflare Wrangler/workerd 产物（34891）和独立 Bun 产物
  （34892）：`/login`、`/apps`、`/logo.png`、`/favicon.png` 返回 200，旧
  `/omi-white.webp` 返回 404。两端标题分别是 `Sign In to Eddy` 和
  `Eddy App Store - Discover AI-Powered Apps`。
- CUA 浏览器验证 Cloudflare Home hydration 后标题仍为
  `Eddy - 懂你的随身AI伴侣`，侧栏与活动图标为 Eddy mark，帮助页显示 Eddy
  support。目标编辑框输入 `Organize my Omi notes` 后保持原文，取消未提交。
  两端应用页页脚显示品牌 tagline，下载分别指向 API 34890 和 34880；修正后
  的装饰代码显示 `createApp`。Server 登录模态框渲染了 Eddy 和账号密码表单。
- 最后返回首页时，原 CF 本地 API 已退出，外层日志报告 `local runtime stopped`，
  浏览器保留会话并显示重试入口。按原工具重新启动全新的
  `web-presentation-api-20260906-c` 隔离实例，原数据目录保留；通过界面创建
  合成账户后，最终 `-c` Web 产物成功进入 Home，品牌标题及图标正确。没有
  放宽测试工具现有的一小时运行期限，也没有把临时服务退出改写成登录成功。
- 四个核心后端提示词/工具文件相对 `9b7e48dca2` 字节一致；两份最终 stage
  的 `src/lib/geminiLive.ts` 与原文件字节一致。变更文件扫描未发现 MiMo
  密钥模式，也没有上游文件交集。`git diff --check` 通过。

私有日志根目录：`/Users/macstudio/.codex/eddy-production/`。
最终证据包括 `web-presentation-checks-20260906-d.log`、
`web-presentation-upstream-20260906-c.log`、
`web-presentation-build-{cf,server}-20260906-c.log`、
`web-presentation-http-20260906-c.json`、
`web-presentation-integrity-20260906.json`；浏览器可见状态及截图见本次任务。

格式使用已验证的锁定 Prettier 与 Python formatter。上游 pre-commit Web
格式检查要求本项目已退役的 package-lock，因此本次手动执行锁定格式检查后
仅使用 `OMI_SKIP_WEB_FORMAT=1`，没有新建或修改锁文件。

## 证据边界和后续

本轮没有完成 Server Web 跨源登录验收：现有本地 Auth 和后端允许来源尚未
配置到 Web 34892，渲染表单不等于登录成功。Cloudflare 本地 API 使用受控模型
夹具；首页 `Hi! I'm Mira` 来自该夹具的 persona 数据，保持原文，不能作为
真实 MiMo 或 Workers AI 推理证据。

MiMo 真实业务验证见 [Server MiMo 记录](server-mimo-verification-2026-09-06.md)。
本次只读核对运行容器仍选择 `mimo-v2.5`、`mimo-v2.5-asr`、`mimo-v2.5-tts`
及 China Token Plan 端点；没有更改默认提示词或供应商配置。

下载地址验证了目标选择，不代表可下载签名安装包；政策和支持 URL 来自
manifest，生产可达性和政策页面内容尚未在本轮验收。两个 artifact 仍明确
`release_ready: false`。Cloudflare 生产发布、完整双目标业务链、旧 schema
升级与 macOS 签名/公证/登录录音验收继续由当前交付任务负责。本轮没有进行
远端发布或更新生产指针。

整个分支此前仍有 `desktop/macos/docs/desktop-updates.mdx` 的既有上游触碰
超预算（4 行 / 1 行）；本轮未更改该文件或放宽门禁，不宣称全分支门禁通过。
