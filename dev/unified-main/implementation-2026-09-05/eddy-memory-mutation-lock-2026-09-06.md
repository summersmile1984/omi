# Cloudflare 记忆写入锁：修复与验证

基线 `e07a9f7a6b`，工作分支 `codex/unified-delivery`。排查记忆历史和撤销
的前置条件时发现：原生正文、可见性、评价和 MCP 编辑没有统一检查
`is_locked`；开发者编辑和桌面状态虽然预读检查了锁，写入前仍有竞态。
冲突确认还可能先更新候选，再尝试修改已锁定的冲突记忆。

## 修复边界

App D1 migration `0161_memory_mutation_lock.sql` 把交互写入许可放在
`cf_memories` 当前行的 UPDATE 触发器。所有现有交互字段共用这一判断，
包括正文、可见性、评价、桌面已读/忽略/基线，以及冲突关闭字段。
它在实际事务内检查旧行的锁，避免请求预读之后加锁仍可写入。
触发器拒绝使 D1 batch 的记忆更新、向量 outbox 和确认记录一起回滚。

原生、MCP、开发者、冲突处理器使用同一个错误翻译模块，把明确的锁拒绝
映射为既有付费边界 402；其他数据库错误仍为 503，不回显底层异常。
删除只更新隐私墓碑字段，账户清除执行 DELETE，因此锁定不阻止数据删除。
没有更改默认提示词、模型选择、提取规则或上游源码。

Failure-Class: FC-split-mutation-authority

这沿用现有失败类的单一写入所有者约束；注册表引用的合并实例为
PR #9365、#9597。本次防护本身是共享数据库写入原语，并非额外静态检查器。
当前缺陷也已通过执行真实路由和 SQL 的旧行为复现确认。

## 验证结果

- 新增 `test_memory_mutation_lock.py`，进入现有 Core pytest 发现路径。
  应用完整 App SQL 迁移链，执行真实 FastAPI/ASGI 路由和 SQLite 事务。
  **35 passed**：七类写入口的预先锁定/预读后锁定、正常旧账户写入、
  跨账户 404、依赖错误 503、隐私删除，以及冲突确认/拒绝/修正回滚。
  还验证拒绝不发布后台消息，正常 MCP/开发者更新提交后才发布。
  MCP/开发者身份使用受控认证接缝；这组测试不证明真实委托凭证链路。
- 仅在私有复现实验中撤去新触发器，执行相同路由回归：**11 failed、
  3 passed**，失败均为本应拒绝的更新得到 200。撤去触发器的代码不进入
  仓库、运行器或部署。该实验与完整绿色套件分开保存。
- Core 完整组件：`uvx uv==0.12.3 run pytest -q`，**498 passed**，
  一项既有 Starlette/AnyIO 弃用提示。随后补充消息发布断言并重跑全部
  35 项新回归通过；生产代码此间未改动。
- Cloudflare `npm test`：**111 files / 884 passed**；`npm run typecheck`
  通过。没有新增脱离现有本地/CI 通道的测试脚本。
- 同一 `contracts/deployment/core.py` 经公开 HTTP 在真实 Server Docker
  以及新建 Cloudflare 本地 workerd 环境分别 **13/13 passed**。
  新用例通过公开注册创建账户，创建手工记忆，编辑正文、可见性、评价、
  已读/忽略/基线，再读回并验证跨账户拒绝和删除后不可恢复。
  不导入后端处理器，不向业务库植入记录，不绕过认证。
- CF 环境正常编译 Worker，应用完整真实迁移并启动 Auth/Core/Edge 等
  绑定。运行器完成后正常清理自有进程。Server 复用隔离的正常镜像
  `memweft-contract-a44e046d403a-api`，未热挂载或改造业务容器。

公开 HTTP 用例没有管理接口可为记忆加付费锁，因此 402 和晚到锁竞争的
行为证据来自 ASGI/SQLite 回归；本次未把它描述为 hosted D1 生产测试。
CF 本地运行器仍使用受控推理。手工记忆编辑通过也不能证明历史撤销、
向量索引完整版本语义、Workers AI 模型质量或生产账号迁移完成。

## 剩余工作与证据位置

历史/撤销仍需追加式版本链、精确幂等身份、当前尾版本的隐私判断与
索引版本所有权。路由清单仍为 **583 staging-owned / 36 blocked**；
本轮没有减少阻塞计数、生成虚假 qualifier 或发布生产 Worker。

私有原始记录在 `/Users/macstudio/.codex/eddy-production/`：
`memory-lock-{focused-final,baseline,core,vitest,typecheck}-20260906.log`，
`memory-lock-server-20260906-a/{core.log,trace/core-results.json}`，
`memory-lock-cf-20260906-a/trace/core-results.json`。
测试账户身份与业务响应不纳入仓库。
