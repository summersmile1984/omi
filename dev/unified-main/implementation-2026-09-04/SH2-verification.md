# SH2：PostgreSQL 删除与完成回执边界

基线：`9893e7bd83`，隔离分支 `codex/implement-server`。本包只改变 fork-owned 文件；不修改上游 users/services，不修改 Auth server，不部署生产。

## 修复的实际边界

2026-09-04 当前 main 的 PG transaction suite 是 20 通过、2 失败。失败不是可通过补两个别名解决：实际 `users.delete_user_data` 仅遍历用户子树，顶层归属记录/孤立后代会漏删；完成函数仍保留 UID、删除反馈等活动状态。旧测试还引用已不存在的 receipt/helper API。本包把测试迁移为调用注册后的真实 `users` 公共入口，保留原有擦除和最小回执期望，未向上游模块添加过时别名。

`firestore_pg/erasure.py` 在单个 SQL 事务里遍历迁移 registry，擦除完整用户 namespace、根 user document 和显式 uid/user_uid 顶层归属记录；对物理表映射、UID path segment、冲突归属 fail closed。根 users 的 document ID 是其 authority，字段不能让另一个用户根记录被误删。账户删除、回执、法律保留和删除租约四类顶层控制记录保留，保证 worker 的 gate 能完成。

`fork/account_deletion.py` 和 registry 接管已有生命周期入口：完成前要求归属记录为零、无 late VM 清理；一个 PG serializable transaction 写最小 HMAC receipt 并删除带私有内容的活动 marker。状态、任务解析、intent、失败/运行/取消、反馈、billing 重放共享该 authority。late cleanup 仅恢复含原 opaque job ID 的最小活动状态。旧用户无 marker/receipt 的正常访问不变；旧 running marker 缺 job ID 可生成 ID 完成；畸形 receipt 拒绝放行。

真实 worker 使用动态路径访问 `legal_holds` 与 `legal_hold_deletion_gates`，旧 PG inventory/migration 未覆盖。不可变 v1–v3 保持原样，v4 仅添加这两个控制 collection。

## 本地验证

日志目录：`/tmp/memweft-implementation/server/`。仅使用自己创建的 `memweft-sh1-postgres`、Redis、Compose project 和合成数据。

- `make setup`：本分支首次提交前已成功；当前包复用锁定 backend venv 和 Git hooks。
- `PYTHON=<backend/.venv/bin/python> BACKEND_UNIT_TEST_FILE_LIST=... bash backend/test.sh`：37 项通过（删除 10、startup 23、real seams 4），见 sh2-focused.log。
- 实际 PG：`run-sh2-live.py` 驱动 `python -m pytest firestore_pg/tests/test_transaction_semantics.py -v --tb=short`，25 项通过，包含既有 22 项。新增法律保留租约延续、stale mutations 不能重建私有 marker、回执写入后删除 marker 故障的真实 PG 回滚。
- 同一 PG suite 调用真实 `background_wipe_user_data`，法律保留、用户擦除、完成回执、租约结束均真实执行。身份/账单/VM/对象/向量/遥测 seam 使用合成 no-op，测试明确验证调用发生；这不是外部 provider E2E。
- `sh2-migration-probe.py`：独立 fresh DB 与 v3 DB；fresh/repeat/check，v3 admission 拒绝、v4 升级保留所有既有映射和 sentinel、仅增加两个 collection、重复迁移验证，见 `sh2-migration.log`。初版外部 probe 写错 registry 表名而失败，保留在 `sh2-migration-first.log`；修正引用生产 `COLLECTION_TABLE`，没有为了测试修改产品。
- 真实容器/API：`sh2-live-api.py` 在当前源码启动的 API 上使用实际 Auth 服务签发的 JWT，普通用户 200 → pending 403 → 最小 receipt 403 → stale cancel/billing 仍 403，用户归属记录零，随后删除合成 Auth/PG 主体（sh2-live-api.log）。
- `backend/Dockerfile` 的当前 linux/amd64 base 构建成功。标准 fork layer 在下载 unchanged pinned sherpa-onnx 时 PyPI TLS/timeout 重试耗尽而 exit 1（sh2-fork-build.log），未改依赖/hash。运行验证使用此前成功的 `memweft-server:auth-consumer` 依赖镜像 + 当前 fork/firestore_pg 源码 overlay，并保留已生成 self_hosted.local profile。该临时 Dockerfile 和日志在外部证据目录，不是 production build 成功证明。首次 overlay 误覆盖为工作树生成的 omi_cloud table，入口按设计拒绝；修正测试 fixture 保留目标 profile 后 /ready healthy。
- Hermetic SQL fault 在第二个 collection 删除时触发，全部擦除回滚；receipt/marker 单事务故障、legacy principal、late cleanup、弱 key/坏 receipt、归属冲突和跨用户 path 均有行为覆盖。

## 验收边界和下一步

回执只证明本 owner 的数据库擦除和 upstream worker 已成功返回其前置清理阶段。它不证明 Auth SQL、备份、对象存储、向量库和任意 provider 的实际擦除。真实当前 worker 的身份删除仍调用 Firebase API，Better Auth CRUD adapter 待 AUTH/SH 后续包；不能把 provider stub 测试称为完整账户删除成功。

法律保留、删除 gate 是独立控制权，保留 UID 的记录仍有各自保留策略。擦除仅支持明确的 user namespace 和 uid/user_uid 顶层归属契约，不会猜测未知字段的所有权。后台写入必须遵守上游删除/法律保留 gate；不声称单次 SQL 扫描可以阻止所有晚到 provider 写入。

Receipt ID 使用 domain-separated ENCRYPTION_SECRET HMAC。部署必须稳定保存此 key；更改 key 需显式迁移 receipt/data，不能直接换环境变量。完整 key rotation 与 provider erasure、cutover/restore 仍需后续验证。

正式全范围 upstream preflight 使用 staged candidate object（不移动分支）；OpenAPI runner 首次在线 uv 同步因网络失败，改用现有锁定缓存 `UV_OFFLINE=1` 完整执行，不跳过检查。`sh2-candidate-preflight-network.log` 保留首次失败，最终 24 upstream + 3 fork 全通过；history failure-class guard 因 shallow clone 明确 SKIP。

Failure-Class: FC-split-mutation-authority。复用现有类，未新增或改变 registry 生命周期。现有 fork startup check 的 local/CI lane 增加此行为 guard；真实实例为 fork PR #7 后当前 main 审计复现的 PG 删除缺口。
