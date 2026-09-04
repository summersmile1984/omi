# Redis worker 重试与终止回执验证

基线：`a9f665d0250790aa4d1668d1d17cba9bd2e152e2`（generation/lease 派发修复）。
本轮第一提交 `8f7efeed1d` 增加认证后的 delivery attempt；紧随修复保留拒绝回执。
两者仅修改 fork-owned 代码，不改变 PostgreSQL finalizer 的 lease/generation 所有权。

## 实际问题与边界

2026-09-04 的真实本地录音，遇到 Qwen 模型进程被 Docker VM 杀死后，
`utils.cloud_tasks_redis._worker` 总是重投同一 envelope，而认证函数固定返回零。
实际 PG job 的 `attempt_count` 到达 20，`task_retry_count` 仍为 0。
因此原有 `routers.conversation_finalization` 的 `final_attempt` 永远为假。
日志：`/tmp/memweft-implementation/server/llm/resume-q8.log`、`queue-model-full.log`。

第一提交让 envelope 保留 `retry_count`，成功验证当前路由的独立 secret 后才接受
`X-Omi-Queue-Retry-Count`。旧 envelope 和旧已认证请求缺少计数时从零开始；负值、
非整数、重复 header 与超出当前队列预算均拒绝。四个队列共用 `Queue.max_attempts()`，
沿用现有 `SYNC_TASKS_MAX_ATTEMPTS`、`ACCOUNT_DELETION_TASKS_MAX_ATTEMPTS`、
`LISTEN_FINALIZATION_TASKS_MAX_ATTEMPTS` 的默认/覆盖关系，配置范围为 1–100。

复核又发现任何非 5xx 原先均被消费后丢弃：错误 secret 的 403、错误路由的 400、
重定向等都被误当成功。第二提交按真实 handler 契约分类：

| 回执 | worker 行为 |
|---|---|
| 2xx（含 finalizer `dropped` / `acked` / `dead_letter`） | 确认消费 |
| 409（锁占用、lease busy、completion conflict）、429、5xx、HTTP transport failure | 持久化递增计数后重投；预算耗尽时保留 |
| 其他非 2xx（含 301/400/401/403） | 保留 envelope 与 `delivery_failure=http_<status>` |
| 非法或已经耗尽的 envelope | 保留原 envelope，不发送、不循环 |

保留位置是同一个 Redis queue 的 `:dead-letter` list。它不是业务完成标记，
也不是新自动重放服务；PG reconciler 仍按自己的 durable job 状态恢复。
此包没有声称修复 BLPOP 后进程崩溃的全部传输持久性，也没有把一个 dispatch generation
的预算误报为跨所有 reconciler generation 的总重试预算。

## 可复现验证

日志目录统一为 `/tmp/memweft-implementation/server/llm/`。

- `BACKEND_UNIT_TEST_FILE_LIST=.../queue-retry-tests.txt bash backend/test.sh`：
  第一提交 14 retry +23 startup +3 dispatch；加拒绝分类后 18 +23 +3，均 exit 0。
  `test_queue_retries.py` 真正执行 `_worker`、认证依赖及现有 FastAPI finalizer handler，
  控制的是处理失败与存储边界。三次调用观察到 `final_attempt=false,false,true`、
  两次 retry 状态写入及一次 final-attempt terminal acknowledgment；没有用源码字符串断言。
- 第一提交精确候选 `c56ff8b7419f89a2e2f0150eb2cb88b718f30118` 的 tree
  `1f1b18596b0b7834f9cf76e9cc317fd3baa58501` 与正式提交相同。
  24 upstream +3 fork checks 全部 exit 0（`queue-retry-upstream-gate.log`、
  `queue-retry-fork-gate.log`）。90-day failure-class history guard 因 shallow clone 明确 SKIP。
- `queue-wire.py` 在实际 linux/amd64 backend 镜像中运行真实 worker 子进程，
  source mount 指向本次 fork-owned queue；连接现有隔离 Redis 与真实 API/PG。
  随机专用 queue prefix 的不存在 opaque job +正确 secret 得 HTTP 200 dropped；
  错误 secret 得 HTTP 403，原 envelope 在 dead-letter 中恰有一份。
  未 seed transcript/memory，未改业务 job；结束只清理该随机 probe prefix。
  `queue-wire-2.log` exit 0。首轮 helper 没设置 `/app` PYTHONPATH 而失败，保留
  `queue-wire.log`，不计作通过。该 wire 证据是源码挂载，不是新不可变镜像证明。

## 仍由模型包承担的预算

原 worker HTTP timeout 为 30 秒，原云端结构化调用 timeout 为 60 秒；本地 CPU
prefill 可更久。所选 LLM 包将通过同一 profile 投影单次模型 deadline、聊天首事件/
总时长和 finalizer HTTP 等待预算，并相对现有 1500 秒 PG lease 保留余量。
本轮队列状态与拒绝分类提交本身不表示本地模型、canonical memory 或完整录音链路已通过。
