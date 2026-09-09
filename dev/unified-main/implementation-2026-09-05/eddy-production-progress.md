# Eddy 首次生产交付实测记录

日期：2026-09-05。执行时源码位于 `codex/unified-delivery` 独立 worktree，基于
`aad3777a68` 加本次改动；之后分为品牌 `fa883e8939`、原生包 `2988cc9493`、
路由登记 `c11c30e40a` 和策略观测修复 `1b40c4dca1` 四个本地提交保存。
用户已授权 Cloudflare 生产部署；该授权
不等于运行或业务验收结果。

## 当前结论

Cloudflare 资源层已验收，但尚未生产发布。18 个数据资源已真实创建，
3 个 Vectorize 元数据索引与 2 条 R2 生命周期策略均已查询确认。
Workers、远端 SQL 迁移和生产账户链路尚未执行。
CF-4、CI-1、prior-schema 三个发布资格执行器仍缺失，正式 apply 会拒绝发布。
本地测试通过不能替代这些证据。

## Eddy macOS

- 原创 SVG 标志位于 `brand/eddy/assets/`，品牌名称 Eddy，标语
  “懂你的随身AI伴侣”。品牌 manifest 选择实际账户的 Cloudflare production URLs。
- 完整 release 编译、嵌套 Developer ID 签名、严格签名校验及依赖审计通过。
  最终本地产物为 `/private/tmp/eddy-macos-production-20260905-b/Eddy.app`，
  版本 `0.1.0 (2026090502)`，bundle ID `dev.summersmile1984.eddy.macos`，
  team `YTY9KEWQC5`。编译日志 `/tmp/eddy-macos-production-20260905-b.log`；
  完整构建和签名记录为产物目录下的 `artifact-manifest.json`。
- 已使用真实 macOS 窗口打开该包：标题 `Eddy v0.1.0`，标志、名称、标语显示正确；
  登录/创建账户表单切换正常。未用源码测试代替这一步。尚未注册或登录生产账户。
- 第一个真实安装包暴露窗口仍叫 omi、SwiftPM 资源进入 macOS bundle 布局后图片
  不可见两个问题；本次在生成器所有权边界修复，新增实际编译标题函数和 AppKit
  图片加载回归测试。该旧包 `...-a/Eddy.app` 不应交付。
- 已签包依赖本机 libwebp，其实际最低系统版本为 macOS 26.0；包据实声明。
  尚未完成 notarization、跨架构、旧系统兼容或生产服务验证。记录中的
  `release_ready`、`service_verified`、`notarized` 均为 false。
- 此包只完成选定品牌资源与认证入口，其他上游 onboarding、设备媒体和内联标志
  仍需逐个消费者验收；不宣称整个客户端已无品牌泄漏。

## Cloudflare 真实操作

私有证据目录 `/Users/macstudio/.codex/eddy-production/`；候选和操作 journal
含完整来源及资源身份，应用 secret 仅存在该私有目录，不写入仓库或日志。

1. `candidate-20260905-d` 完成本地 8 组检查、两目标 Web 构建、SQL fixtures、
   8 Workers 编译与冻结上传 dry-run。仅 `local_verified=true`。
2. `provision-20260905-a` 实际创建 2 D1、5 R2、4 Queues、7 Vectorize，共 18 项。
3. 第一项 Vectorize `created_at` 元数据索引创建进程 exit 0；只读 API 查询确认
   索引存在且 `indexType="Number"`。原适配器按文档的 `"number"` 比较而拒绝，
   journal 保持 `reconciliation_required`，没有据此重试创建或将其写成成功。
4. 适配器增加明确枚举映射及错误类型拒绝测试。`provision-20260905-b` 又发现
   元数据索引创建的异步传播：创建 exit 0、立即 GET 为空、稍后 GET 确认存在。
   修复为最多十次、间隔两秒的只读确认，不重复创建；权限、传输和类型错误立即失败。
5. 最终 `candidate-20260905-f` digest 为
   `46e8a10cf9288f51047978add119e1d3cdc078084657352db8c7fe49f689713e`。
   `provision-20260905-c` 在观察阶段收到 HTTP 401，零写事件；保留其 journal。
   后续新事务 `provision-20260905-d` 确认全部 18 项并补齐剩余三条策略，状态
   `provisioned_requires_new_candidate`，`release_ready=false`。没有重放旧事务。
6. `preconditions-20260905-a.json` 记录再次只读查询：全部 5 条策略、实际账户
   workers.dev 路由和所需 secret 引用可用。secret 尚未上传到 Workers。
   `worker-schema-observation-20260905-a.json` 记录全部 8 Workers 为 absent；
   Auth/App D1 各仅有平台 `_cf_KV` 表，未执行任何业务 SQL 迁移。
7. 实际 canonical CLI `apply` 对候选 f 返回 exit 1：
   `release qualification pending: CF-4, CI-1, prior-schema`。未创建 apply journal，
   未发起迁移或发布。日志为 `apply-qualification-check.log`。

上述候选属于提交前的精确源码内容；任何后续代码或提交变化都必须重新 prepare，
不能把本次候选结果直接用作后续提交的发布资格。

## 本地验证与其边界

- Brand tooling：39 tests；deployment profiles：8 tests，均通过。
- macOS 原生 lane：Swift 12、Node assets 2、Python staging 17；完整重跑日志
  `/tmp/eddy-native-verification-20260905.log`，最终 exit 0。
- Cloudflare 全部 110 files /861 tests、Core 415、AI 131 均通过；两目标 Web、
  SQL fixtures、8 Workers 编译与冻结 dry-run 全过。日志在候选 f 的 `logs/`。
- 其中 release adapter/transaction 46 tests 通过，包括真实线上 `Number` 和文档
  `number` 两种返回值、错误类型、异步可见、权限错误、超时后禁止重复写入；
  单元测试本身仍是可控 API 边界证据。
- 上游路由重新枚举为 619，576 staging-owned、43 blocked。新发现的 7 条每日
  使用/摘要写入与反馈后台接口均据实登记，未用清单变绿宣称实现完成。
- Redis 边界重新分类：每日摘要锁、旧 Windows 列表限流、增量摘要预算仍有缺口。

正式完成需要：固定的产品/双目标/旧 schema 执行器产生当前候选证据、正式
迁移与发布、线上注册/登录/数据持久化/录音与派生业务/聊天及拒绝路径，以及
Eddy 原生客户端对真实生产服务的认证、恢复、录音和退出验证。
