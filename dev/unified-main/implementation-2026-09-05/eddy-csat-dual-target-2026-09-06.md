# Eddy 满意度接口：双目标实现与实测

2026-09-06，基于 `94302bbc30` 增加 Cloudflare 的 `/v1/csat/config` 和
`/v1/csat/ratings`，同时修复真实 Server 请求发现的 PostgreSQL schema 缺口。
上游 `backend/routers/csat.py`、`backend/database/csat.py` 和默认 LLM
提示词保持不变。没有发布 Cloudflare Worker 或执行远端 D1 迁移。

## 行为与数据所有权

Cloudflare 的 Core/D1 现在提供产品配置单例、缺失配置默认值、管理员配置
规范化，以及每个 UID/平台只能创建一次的评分。配置默认文案使用当前品牌；
管理员显式配置的文案保留原值。参数校验、字段截断、服务端高分丢弃评论、
首次提交 201 和重复提交 409 均沿用上游接口契约。D1 唯一约束与不可变更新
触发器保留第一份答案；调用方不能通过 body 的 UID 改变评分所有者。

迁移 `0158_csat.sql` 建立两张表，使用现有账户删除 intent/tombstone
边界拒绝迟到写入。评分已接入公开导出、Jobs residual purge 和只读隐私
检查器。配置单例属于运营配置，不随某一用户账户删除。

同一组公开 HTTP 用例首次运行在 Server 时，配置与评分分别返回 500。
日志确认 `SchemaNotCurrent`：常量构造的 `csat_config`/`csat_ratings`
没有纳入 schema。新增版本 9，保留版本 1–8 不变；真实业务所有者进入
已有 schema-admitted 回归夹具，另以真实 PostgreSQL 验证并发首次创建、
重复提交不覆盖和按 UID 删除。此修复属于
`FC-dynamic-document-path-missing-schema-inventory`。

## 实际执行

- Cloudflare 全量 Core：固定 uv/Workers Python 环境执行 `pytest -q`，
  **445 passed**。相关新路由与导出测试单独执行 **11 passed**。
- TypeScript 类型检查通过。初轮全量 Vitest 为 879 passed / 1 failed，
  检查指出新表未使用统一的 INSERT/UPDATE 删除 fence 声明。补齐后相关
  residual suite **6 passed**；最终全量 **111 files / 880 passed**。
- Server 正常镜像内通过 `backend/test.sh` 执行 startup、PG owner inventory、
  self-host config 三个现有文件，禁用网络，**46 passed**。
- 独立、用完销毁的真实 PostgreSQL 上，`test_transaction_semantics.py`
  **31 passed**，包括 CSAT 并发 create-only 与删除测试。首轮未设置测试进程
  的导入期 `ENCRYPTION_SECRET`，一个既有 wipe 测试因此失败；正确提供隔离
  测试配置后全量通过，没有修改该既有测试来避开失败。
- 本地保留数据的 Server 从 schema 8 升级到 9，正常构建镜像
  `memweft-contract-a44e046d403a-api`，四个应用服务健康。原有 7 条记忆和
  completed 录音保留，录音结束位置仍为 9.44 秒。
- 更新后的同一 `contracts/deployment/core.py` 在 Server 和真实本地
  Cloudflare Workers 上均 **10/10**：配置/校验、评分/隔离与原有
  登录恢复、onboarding、任务、撤销凭证用例。源代码没有数据库预置或认证绕过。
- Cloudflare 最终新建运行的录音/隐私 **13/13**，还执行评分提交与导出、另一个账户不可见、实际
  Queue 删账户后评分行清零。ASR/LLM 仍为此本地夹具的受控 IO，不能当成
  托管模型质量或已部署环境证据。
- 路由清单核对 **619** 个上游注册槽，现为 **580** 个 Worker owner、
  **39** 个待实现槽；Cloudflare route manifest **632** 项通过检查。

证据根目录：`/Users/macstudio/.codex/eddy-production/`。主要文件为
`csat-server-20260906-{a,b}/core.log`、`csat-server-upgrade-20260906.log`、
`csat-backend-runner-20260906.log`、`csat-pg-tests-20260906-b/tests.log`、
`csat-cf-20260906-{a,b}/trace/`、`csat-cf-core-full-20260906.log`、
`csat-cf-vitest-final-20260906.log` 与 `csat-route-registry-20260906.log`。

## 验收边界

这是一个完成双目标接口实测的业务族，尚不实现整个 CF-4 或 CI-1 发布执行器。
Server CSAT 沿用上游默认文案，其白牌文案投射尚未验证；本轮没有把双目标
HTTP 数据一致性表述为全客户端白牌完成。管理员反馈报告、推荐邀请、其余
39 条路由、完整生产发布和 Eddy macOS 生产联通仍需继续完成。
