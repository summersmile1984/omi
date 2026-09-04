# CF4：指定聊天会话的文本 HTTP / SSE 闭环

基线：`f216538cd0dff6b05101b31515dc31563d75c7a0`。本包只在短期
`codex/implement-cloudflare-product` 上本地提交；没有远端资源、秘密上传、D1
remote migration、Worker deploy 或发布批准。`release_qualified=false`。

## 真实缺陷及权威契约

当前 `web/app/src/lib/api.ts` 的 `getMessages`、`sendMessageStream` 和
`clearMessages` 都发送 `chat_session_id`。当前上游
`backend/utils/chat_session_target.py`、`backend/routers/chat.py` 指定：显式 ID
必须按 UID 查到真实会话，缺失/他人 ID 返回 404；所选会话的 app 优先于 query；
显式清空保留会话，默认清空删除当前会话。本包没有修改这些上游文件。

真实本地旧代码复现：用户一 A/B 各一条消息，GET A 返回 A+B；用户二请求 A
却得到自己的 C；用户二 DELETE A 还把自己的 C 删除。这是选择权威丢失，不能
用放宽客户端 404 或返回默认线程掩盖。原始证据：
`/tmp/memweft-implementation/cloudflare/chat/initial-probe.{log,json}`。

## 实现边界

- `python/shared/chat_target.py`：不可变 UID/session/app/clear-epoch 目标，
  Core 读取/清空与 AI 生成/提交共用；删除重复的 default/initial/completion
  选择及提交实现。显式查找失败发生在 quota/provider 前。
- `0155_chat_clear_epoch.sql`：普通 forward migration；旧会话 epoch 默认为空，
  显式 clear 轮换。模型等待前捕获目标，提交整个回复时在 D1 batch 内条件写入；
  clear/delete 后晚到结果没有消息写入，也不重新创建旧 session。模型成本仍按
  已发生工作结算。真正新会话可在其首次成功提交时创建。
- Core 的显式 clear 保留会话、重置 count/preview，GET 限定同一 UID/app/session，
  空 offset 页返回空列表。没有会话的旧主体仍可读/清空原 app-scoped 历史。
- 固定 Python 入口把唯一普通 shared 源复制到两个私有隔离 source stage，
  Wrangler 收集为普通模块。原源码、锁文件、CF5 sourceIdentity 的禁止源链接
  规则和 frozen artifact 的禁止链接规则保持原样。stage 自动清理，dev 需要
  重启以消费新的源快照。

## 可重放验证

日志目录：`/tmp/memweft-implementation/cloudflare/chat/`。所有账号/文本均为
该目录所属的新本地 fixture 合成数据；报告不含 token/密码。

```sh
uvx uv==0.12.3 run --project deploy/cloudflare/python/api-core pytest deploy/cloudflare/python/api-core/tests -q
uvx uv==0.12.3 run --project deploy/cloudflare/python/api-ai pytest deploy/cloudflare/python/api-ai/tests -q
node deploy/cloudflare/node_modules/vitest/vitest.mjs run --root deploy/cloudflare tests/python-worker.test.mjs
CLOUDFLARE_PYODIDE_CACHE_DIR=/tmp/memweft-implementation/cloudflare/runtime-cache \
  node deploy/cloudflare/contracts/local-target.mjs \
  --output /tmp/new-owned-chat-target --brand-id chat-cf --run-core --run-recording --run-chat
```

- Core 全套：407 passed，`core-full.log`；AI 全套：121 passed，`ai-full.log`。
  新测试直接执行生产 Core/AI owner 与 SQLite：冲突 app、缺失/跨 UID、旧主体，
  以及 provider seam 内执行真实 Core clear/default delete 后返回模型结果；
  后者为 503、零消息、应保留/删除的 session 状态正确、费用记录保留。
- Python 入口：8 passed，`source-projection-tests.log`。普通共享源 dry-run：
  `projected-source-dryrun.log`；生成的 `chat_target.py` 是普通文件。
- `formal1` 是首次聊天原始客户端/RPC 5 case 实证；`formal2` 是普通 stage
  构建的完整 Core/录音/聊天组合，结果见其 `logs/{core,recording,chat}.log`
  与 `trace/*-results.json`；最终精确候选的正式门禁另记录在提交消息。
- `chat.mjs` 直接 bundle 当前上游 Web API 模块，唯一客户端 seam 是真实公开
  注册所获 JWT 与 `/api/proxy` 基址映射；未改请求体、查询、SSE 解码或历史
  响应。普通 `data:` 文本与 base64 `done:` 的中文/换行完全一致，done 中的
  message ID 与 D1 历史一致。模型 RPC 故障是真实 Worker service-binding
  抛错，响应 502，历史没有半个 exchange。
- `bash deploy/cloudflare/ci/product.sh` 在 fork manifest 的 local/ci 两 lane
  执行同一组合；上游 Web lib/types 变动也触发本 CF consumer 检查。既有
  routes lane 同时运行 Core 和 AI 全套，包含并发晚到输出回归。

## 未证明项

这些证据不是浏览器 UI、全部聊天协议或发布资格。ASR 与模型内容/usage 由
无应用存储权限的 provider fixture 控制；D1、Auth/JWT、路由、SSE、模型 RPC、
DO、Queue/Jobs/Core 都是真实本地组件。没有证明托管模型质量、token-by-token
延迟、工具/MCP 执行、附件/图片、所有 BYOK 行为、36 个 blocked route、所有
原生平台或完整 Server/CF wire parity。默认首次请求还未建立可见会话前的
并发 clear 不是本包已证明的显式会话生命周期；显式 A 的 clear/delete 有
直接生产回归。历史/远端 schema 发布与回滚资格仍 pending，需要实际证据和
用户显式发布授权，不能手写 approved=true。服务端默认问候/系统提示仍有
上游品牌文字；这里不把文本链路通过冒充白牌产品文案已完成。
