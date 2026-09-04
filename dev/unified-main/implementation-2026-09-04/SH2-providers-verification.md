# SH2：Server provider 真实消费者与回执写入边界

本包基于 `e6adb7dae5`，在 `codex/implement-server` 独立 worktree 实施，未改上游文件、未改 allowlist、未发布。完成的是 Qdrant/MinIO 的生产消费者、明确迁移和账户删除 provider 边界；不是整个 SH2 或完整产品上线验收。

## 原始问题与改动

- Compose 配置 `VECTOR_STORE_PROVIDER=qdrant`，但 `database.vector_db.index` 只有 Pinecone 初始化。无 Pinecone 时生产记忆写入直接跳过。现在 fork registry 为 self_hosted 安装 Qdrant index，保留真实上游 upsert/query/update/delete/list 调用形状；omi_cloud 不应用。拒绝同时配置 Pinecone。
- `qdrant-migrate` 是独立 Compose 一次性服务，与 API 共享 prefix/dimension；只有迁移成功才启动 API。`python -m fork.vector_qdrant migrate|check` 为 7 个现有 namespace 建立/校验 Cosine collection，不重建已存在但维度不兼容的 collection。既有字符串 ID 映射 UUID，原 ID 保留在 payload；所有查询谓词保留，未知运算符拒绝执行，不能丢 UID 过滤条件。
- 原 MinIO shim 不接受生产 `_get_signed_url` 的 `version=v4` 与 timedelta，缺失流式写入、size、metadata/reload、cache_control 等音频路径接口，并把任何 HEAD 错误当不存在。现在补齐真实使用面，文件流在超过 8 MiB 后落临时文件；上传端异常不发布半成品。仅明确 404 为不存在，权限/连接错误继续抛出。
- `MINIO_ENDPOINT` 负责内部传输，`MINIO_PUBLIC_ENDPOINT` 负责客户端签名，TLS 由各 URL scheme 决定。GET URL 为 path-style SigV4；Redis 缓存身份包括 public origin/bucket/object/duration，避免同名对象跨桶串用 bearer URL。凭据没有默认值，也不在 Config repr 中显示。
- 现有 external_write_fence 直接读 UID marker，私有 marker 清理后看不到 HMAC receipt。fork 显式替换 canonical 和 4 个捕获 import 别名（共 5 处），使用同一个 receipt-aware 状态 owner。MinIO 的 local stage 也进入真实写入门禁。
- PostgreSQL shared advisory transaction lock 覆盖已准入的 provider 写；整个 wipe 和最终 completion proof 取相同账户的 exclusive lock。同线程嵌套 completion 重用锁。占用时立即返回可重试失败，不能在写入未结束时开始不可逆清理。
- 清理不依赖 PG 内剩余对象 ID：Qdrant 按 metadata.uid 清理全部 namespace；MinIO 按配置的 UID prefix 清理 speech profiles、recordings、private cloud chunks/audio/merged/playback、temporal sync、chat 和可配置 screen frames。再次计数不为零或 provider 无法证明结果，就保留可恢复 marker；原上游 required failure 不会被覆盖为成功。

代码 owner：`backend/fork/vector_qdrant.py`、`vector_filter.py`、`storage_minio.py`、`storage_minio_blob.py`、`provider_objects.py`、`provider_guard.py`，入口在 `patches/`。已有 fork startup 的 local/CI lane 执行新的行为测试；现有 source check 检查 migration 入口和 Compose dependency。该 guard 扩展捕获了真实 fork PR #7/main 审计中的“声明 provider 但实际消费者缺失”和启动闭包问题，不是孤立的检查脚本。

## 验证与原始日志

所有原始日志在 `/tmp/memweft-implementation/server/`，私有 fixture env/JSON 只包含本地合成凭据，未写入 Git。使用自身 `memweft-sh1` internal network、PG16.4、Redis7.4.2、Qdrant1.15.4 和 MinIO Compose 固定 digest，不操作其他代理服务或生产资源。

| 验证 | 命令/日志 | 结果与覆盖边界 |
| --- | --- | --- |
| 正式 hermetic runner | `PYTHON=<venv绝对路径> BACKEND_UNIT_TEST_FILE_LIST=<provider-tests.txt> bash backend/test.sh`；`provider-tests-final.log` | vector/MinIO/provider/startup/config/真实 patch seam 通过；受控 HTTP/S3 错误与已有 principal 无 marker 可写的兼容行为在 CI 执行。正式 preflight 命令和 exit 记入本提交记录。 |
| 标准 Python 镜像 | `docker build --platform linux/amd64 -f backend/Dockerfile ...`，再 `-f deploy/self-host/Dockerfile ...`；`provider-base-build.log`、`provider-layer-build.log` | 使用锁定依赖与正式 Dockerfile，非只覆盖源代码的临时镜像；profile 真实生成进镜像。 |
| 真实 Compose migration/API | `docker compose -p memweft-sh1-compose -f <compose.provider-fixture.json> run --rm --no-deps qdrant-migrate`，再 `up -d --no-deps backend` | migration exit 0，新 prefix 7 collections，API healthy。fixture 复用自己的 PG/Redis/Auth；未运行全套模型/Typesense/SearXNG。 |
| Qdrant 实际版本语义 | `probe-qdrant.py`；`qdrant-semantics.log` | exists=true 匹配空数组与非空数组，false 只匹配缺字段；嵌套元数据更新保留原字段；UID purge 3 条成功。以固定 1.15.4 实际响应为准。 |
| 生产调用链 | `docker exec -i memweft-sh1-compose-backend-1 python < provider-live.py`；`provider-live-2.log`、`provider-live-3.log` | 上游 memory upsert→真实 Qdrant query；仅 embedding 推理使用可控向量。真实录音上传/读取/签名 HTTP GET、Redis 缓存、stream/metadata/reload/list size 通过。 |
| 并发与回执 | 同一 live probe | 真 PG shared writer 占用时，wipe 失败且不可逆 callback 未调用；provider 残留阻止 receipt；所有目标 UID vector/object 消失后 receipt 发布；五处 captured fence 与 local MinIO gate 拒绝新写；其他 UID sentinel 保留。 |
| 整体 wipe 的 provider 子闭环 | `provider-live-3.log` | 实际 background_wipe、PG 删除、legal-hold gate 完成、receipt 同时通过，Qdrant/MinIO 为真实服务；Auth/billing/VM/其他 derived families 受控，不能据此声称这些外部系统清理完成。 |

首次 `provider-live.log` 因 Python Mock 自动检查上游 lazy embedding client 触发未配置 OpenAI 而失败（exit 1）；改为显式注入确定向量后成功。它不是本地 embedding 推理成功证据。此前脚本失败和重试日志都保留。

Qdrant predicate/UUID/payload 的外部契约可参照 [官方 filtering 文档](https://qdrant.tech/documentation/search/filtering/) 与 [官方 upsert API](https://api.qdrant.tech/api-reference/points/upsert-points)；本包仍以固定镜像真实 wire 验证约束版本兼容性。

## 尚未证明、下一包必须处理

1. `deploy/profiles/self_hosted.yaml` 声明 embedding_dims=3072，example EMBEDDING_DIMENSION=1536。generic EMBEDDING_PROVIDER 尚不能配置上游实际 embedding client。下一 SH3 包需统一 profile/模型/Compose 的真实维度；本包严格检查显式维度，不能改一个数字伪造兼容。
2. Auth 删除的真实 worker 仍有 Firebase consumer；billing、VM、Twilio、Typesense/canonical/frame-request provider 未在本包端到端清理。外部 identity 删除归 AUTH-1 的同一 owner，不能再造另一套账户语义。
3. 部分 PG 后台写 owner（projection fence、daily sweep、memory ledger/apply、goals、MCP OAuth 等）仍直接读 UID marker，未消费 receipt。这里的 5 个 provider fence 不代表这些内部写入也全覆盖。
4. 非 UID 布局的 postprocessing/sdcard 临时对象和全局 catalogue asset 不在已声明 owned prefix 集合；独立 frame-request storage owner 的消费者需另核对。必须为真实遗漏路径补所有权或 durable cleanup，不能把用户对象清零简写成整个 MinIO bucket 清零。
5. `public_url` 是 unsigned asset 地址，adapter 不自动开放 bucket。公共 logo/catalogue 的读取政策和把该地址长期保存的上传路径仍需实际产品验收；本包证明的是显式签名的私有录音读取。
6. PG connection 丢失释放锁、外部请求结果未知时不具备跨服务原子事务。还需 durable uncertain-write/reconciliation 策略，不能把普通并发准入证明扩大为故障下绝无残留。
7. 完整备份恢复、切流冻结/回滚和标准 OS 所有 provider 的生产 egress 仍属后续 SH4/SH3。API /ready 与局部生命周期过关均不等于完整部署可交付。
