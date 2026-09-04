# SH3：Redis 录音最终化派发边界修复

基线：`667a3e791492a6fc4617385bf926d691b550d4f2`。本包仅修改 fork 自有
queue admission / transport / captures / tests / guide，不含本地 LLM 特性。

## 实际故障与改变

2026-09-04 的标准 Server 合成测试账户完成真实 Kokoro 语音生成、SenseVoice
识别、PG 转写持久化和公开说话人分配后，`POST /v1/conversations/:id/finalize`
返回 503。原因是 Redis enqueue 已接通，而生命周期的 configured predicate
仍要求 GCP project/location/queue/invoker；这个谓词被 lifecycle、reconciler
和 live-listen 捕获，单独换函数原址无法完成消费端迁移。

`fork/finalization_queue.py` 复用现有 `QUEUES` 的 finalization URL/secret 验证。
Redis target 的 canonical 与全部已知 captured predicates 共用该 owner；缺配置
在创建 outbox 前拒绝。Redis 不可达时保留 queued intent，由既有 reconciler
重试，不回退 inline。Omi-cloud 保持原行为。

传输唯一实现保留在 fork-owned `utils/cloud_tasks_redis.py`。原 finalizer 名称
`fin-{job_id}` 被永久 names set 去重，会丢弃同一 job 的新 dispatch_generation；
SADD 和 RPUSH 两次写之间也可能失效。本包以一次 RPUSH 发布 opaque
`{job_id, dispatch_generation}`。重复交付允许，PG 原有 generation/lease 是唯一
执行去重权威；不新增 Redis 状态，不改变另三类队列。直接 Redis 调用和 patched
canonical/captured 调用都指向同一个 transport。

Failure-Class: FC-split-mutation-authority

这是将同一 finalization dispatch 状态收敛到现有配置及 PG outbox/lease owner。
保留现有 registry 生命周期。行为测试针对本次真实 Server 503 与新代次被吞现象，
加入现有 startup local/CI lane；不新增静态源码字符串检查。

## 验证与边界

日志目录：`/tmp/memweft-implementation/server/llm/`。

- `UV_OFFLINE=1 make setup`：exit 0，`queue-setup.log`。
- 正式 `BACKEND_UNIT_TEST_FILE_LIST=.../queue-tests.txt bash test.sh`：exit 0，
  startup 23 + finalization 3 行为测试，`queue-isolated-focused.log`。测试执行真实
  lifecycle request、canonical/captured/直接 Redis consumer，用可控 Redis/intent
  seam 检查旧 job、重复/新代次、publication 失败保留 queued、缺 credential
  在 intent 前拒绝以及 legacy omi_cloud 不变。它不是实际 PG 并发证明。
- 实际标准 Linux API fixture 的 public finalize：修前 503（`finalize-api.log`），
  修后 200 processing + GET finalization 200 queued（`queue-live.log`）。真实
  Redis worker 调用 `/v1/conversation-finalization-jobs/run`，进入 upstream
  `process_conversation`，模型因共享 8 GiB VM 的全局 OOM 返回失败，实际 handler
  返回 500，`queue-worker-api.log` / `queue-model-full.log`。已停止本包自有 worker
  防止无益重试，没有完成对话/记忆的成功声明。
- 收敛 transport 后重新启动 API，再对前次真实录音执行 public finalize，并向
  真实 Redis 重复发布既有 job/generation，`queue-final-live.log`。无注入转写或
  canonical memory，未修改 PG 的 lease/generation。测试账户、数据、服务全为
  本次隔离夹具；日志不含 JWT/密码。

实际 fixture 使用此前标准 speech amd64 镜像的依赖层与当前 backend 源码只读挂载。
本包 queue 文件与被验证工作树逐字相同，LLM 的未提交特性同时存在，因此这里的
证据限定为 dispatch 入口/实际 worker 到达处理边界，不能称本包新独立镜像、
完整 LLM/录音产品或生产 cutover 验收。最终 LLM 包必须继续新镜像及完整流程证明。
