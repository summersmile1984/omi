# Eddy 截图存储资源与新候选验证

2026-09-06，本轮基于 `4db93c54c898bf6d2d282f0f4506fa7a05c5fb05`
重新构建完整候选，随后通过正式资源事务创建截图独立 R2 桶。
这是生产资源准备进展；Workers 尚未发布，Eddy 生产业务仍未验收。

## 实际完成

- 只读查询确认现有 Eddy 的两个 D1、五个 R2 桶和账户 workers.dev
  子域；没有把另一套 Omi Workers 当作 Eddy 的部署。
- 新私有 inventory 补齐 `screen-frame-writer` 及 referral、screenshot
  签名密钥引用。十二个既有密钥值保持不变，两项新密钥独立生成；
  Core 与截图 Writer 共用截图签名引用，与内部认证密钥分离。
  密钥与完整操作证据保存在仓库外，文件权限为 0600。
- 使用正式 `release.mjs prepare --stage production --brand eddy`
  生成候选 `4546ce98b08540ed9f0d44ef82cadf03e691892790d8fe4b2ddfc52c0672da71`。
  八组检查、双目标 Web 构建、SQL fixtures、九个 Worker 编译与九次
  冻结上传 dry-run 全部通过；随后正式 `check` 再次校验源码和产物身份。
- 正式 `release.mjs provision` 使用该候选的精确 digest，进程 exit 0。
  唯一写事件为 `create:r2:screen-frames`，创建
  `eddy-cf-screen-frames-production`，2026-09-06 14:04:26 北京时间由
  API 读回确认。其余十八个数据资源保留，现为十九个。
- 14:06:50 北京时间再次查询，截图桶为 present，部署前置检查通过。
  首次发布 schema 执行器对九个 Worker 逐个确认真实 absence，对两个
  D1 确认业务 catalog 和迁移 ledger 为空，并执行候选内冻结的
  Auth 10 个、App 169 个 SQL 文件。五项 `first-release.*` 检查通过。
  这些查询没有对远端 D1 执行业务迁移。

## 验证范围

本候选执行的测试包括：Worker 114 files / 923 tests，Core 592 tests，
AI 150 tests；上游 Web 75 files / 419 tests，fork Web 5 files / 27 tests，
以及相应 Bun 客户端和构建器检查。完整命令与每份日志摘要记录在候选中。
Core 仍有既有 Starlette/AnyIO 弃用警告，全部命令 exit 0。

资源计划包含九个 Worker、十九个数据资源和两个 Durable Object namespace，
共三十项；十九个数据资源为 2 D1、6 R2、4 Queue、7 Vectorize。
已创建数据资源不等于 Worker 或 Durable Object 已激活。

资源事务终态为 `provisioned_requires_new_candidate`，`release_ready=false`。
后续发布应使用事务返回的 inventory 重新 prepare，并取得完整 CF-4、CI-1
与部署后 schema/业务证据；本轮没有跳过缺失的验收执行器。
本次文档提交也改变了源码身份，不能拿此前候选直接代表提交后的源码。

原 Gemini 截图审核的实际 Cloudflare 请求仍以 HTTP 402 / code 2021
为最新观测；上游输入范围在托管 Worker 内的内存行为仍待验证。
没有更换审核模型、修改默认提示词或降低输入范围以消除这些未完成项。

## 证据位置

仓库外目录 `/Users/macstudio/.codex/eddy-production/`：

- `candidate-production-20260906-screen-a/`：冻结源码、产物和八组检查日志。
- `provision-production-20260906-screen-a/journal.json`：唯一创建事件与确认。
- `production-preconditions-20260906-screen.json`：新桶读回、九个 Worker
  absence 和五项首次 schema 资格检查。
- `inventory-production-20260906-screen.json`：本轮输入的非密钥资源映射。
- `inventory-production-20260906-provisioned.json`：事务返回的资源映射，供下一次 prepare 使用。

本地产物 `Eddy.app` 仍为 `0.1.0 (2026090502)`，最低系统 macOS 26.0；
本轮确认文件与 manifest 存在，未把这一检查当成新的签名、notarization
或生产连接验收。`service_verified` 和 `notarized` 仍为 false。
