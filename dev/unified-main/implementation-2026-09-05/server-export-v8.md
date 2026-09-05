# Server 完整导出：视觉回执的迁移所有权

2026-09-06。在实际录音已生成记忆后，`GET /v1/users/export` 返回 500。
生产导出函数始终读取 `frame_vision_receipts`，但 PG 迁移清单未包含它，
因此即使账户没有视觉记录，空集合查询也会触发 `SchemaNotCurrent`。

## 修复

schema v8 通过现有迁移锁、注册表和版本账本显式创建该集合，不在请求路径
创建表，也不跳过导出内容。v1–v7 的集合映射和已有账户数据保持有效。
启动仍要求当前版本，已有部署先执行 `python -m fork.migrate migrate`。

近期 onboarding、canonical memory、feedback 三次迁移修复也涉及动态集合
遗漏，本次沿用其共享 `SchemaFirestore` 断言边界，补充有限的集合遍历。
测试执行完整上游 `iter_user_data_export`，不替换两个集合迭代器；因此未来
新增导出集合未登记时也会在现有 CI lane 中失败。覆盖无视觉记录旧账户、
保留的视觉回执、嵌套 goal event 以及其他用户数据隔离。

迁移测试的版本断言从 7 更新到 8，依据是实际上游
`services/users/data_export.py` 的视觉回执读取和本次 500，而非仅随实现改值。
新增真实 PostgreSQL 用例从 v7 状态执行升级，验证旧账户数据保留、空集合
读取、视觉回执写入/读回、跨用户隔离和迁移幂等。

## 已执行验证

- 实际 fork 运行时依赖环境、断网 Docker 进程执行 owner/startup 测试：35 项通过。
- 独立临时 PostgreSQL 和正常迁移/客户端执行 transaction suite：30 项通过；
  测试容器、匿名卷和网络完成清理，没有使用产品夹具数据库跑破坏性迁移测试。
- 新基础镜像从未修改的上游 Dockerfile 正常构建；所有已跟踪 backend Python
  源文件通过镜像内 SHA-256 对比。随后构建新的 fork profile 和 LLM 镜像。
- d 轮夹具保留原卷和数据，停止其应用服务、执行正式 v8 migration，再启动
  新镜像 `memweft-contract-c7c265becd46-api`，实际 readiness 通过。
- 实际 `/v1/users/export` 返回 **200，37,257 bytes**，完整 JSON 含 1 个会话、
  2 条记忆、1 个任务、5 条聊天消息。会话 ID、记忆偏好和任务 ID 对应原录音；
  聊天内容及 sender 的集合与 `/v2/messages` 一致。

私有证据在 `/Users/macstudio/.codex/eddy-production/`：
`server-export-v8-container-unit-20260906-a.log`、
`server-export-v8-postgres-20260906-a.log`、
`server-export-v8-restart-20260906-a.log`、
`server-export-v8-http-20260906-b.log`。导出文件 SHA-256：
`bcf25b89e9ede3d87ab95998834a6f424fb6a3a1e5c0afb3055aa9e035599349`。

## 尚未通过的范围

首次验证探针误将任务 envelope 三字段计成三个任务，也错误假定聊天导出 ID
与聊天 API ID 相同。实际上游 `database.chat.iter_all_messages` 用存储文档 ID
作为导出 ID，API 使用 payload ID；本次保持该上游行为，按正文和 sender
核对数据完整性，不把 ID 差异误报为消息丢失。

导出的下载文件名仍是 `omi-export.json`，Server 白牌边界尚需修复。
真实模型聊天已完成传输和持久化，但回答没有检索到已有偏好。
Firestore 导入对照套件需要额外 emulator，本次未重跑该套件；仅同步其中
旧 schema 版本的断言。上述成果不构成 CF-4、完整 CI-1 或 Eddy 生产验收。
