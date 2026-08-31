# Chat-file staging boundary

截至 2026-08-31，Cloudflare 只承接显式的 staging-only `/v1/cf/chat-files`，并没有切换 legacy `/v1/files` 或 `/v2/files`。

本轮发布同时应用了 `0113_chat_assistant_provider.sql` 与 `0114_gemini_proxy.sql`，API-AI `15a48911-bc8a-41f1-bd40-7a671f5d24e5`、Jobs `969c6d84-1cd3-4c8f-8781-914c44366349`、Edge `261cf422-4a28-4841-b316-cea5711f9d7a` 已上线。Assistant continuity adapter 仍由 `CHAT_ASSISTANT_PROVIDER_STAGING_ENABLED` 显式开启；当前未配置 OpenAI provider secret，临时 Better Auth 账号命中关闭开关时返回 `404 legacy_route_disabled`，因此不代表 legacy owner 已切换。

## Session attachment staging evidence（2026-08-31）

远端 App D1 已应用 `0111_chat_session_files.sql`。API Core `c4305fe3-aea6-450d-ac52-b0be2fcd340c`、Jobs `a5d71c63-8c1f-4b68-a614-5f344b785bba`、Edge `02680195-2ab7-43fe-ae8f-de93ef354ffd` 已发布。隔离 Better Auth 账号创建 chat session 返回 `200`；`GET /v2/cf/chat-sessions/{session_id}/files` 返回 `200 []`，不存在或跨账号 file id 的 attach/detach 返回 `404`，未配置 OpenAI Files provider 时上传返回 `503 provider_unavailable`。公开删号请求返回 `200`，session-file 关联没有残留；本次即时检查仍看到 deletion intent，最终 tombstone 交由异步 residual sweep 完成。

这次只闭合 D1 的 uid/session/ready/deletion-fence reader，不代表 Assistants thread/file_search/run 或历史 Firestore/GCS 回放已完成。

## 已闭合的 staging 子面

- Jobs Worker 校验 Better Auth signed context，并用 bounded multipart parser 限制 10 个文件、单文件 50 MiB、请求总量 100 MiB。
- D1 `cf_chat_files` 保存 uid、稳定请求指纹、provider file id、大小、SHA-256、状态和私有对象 key；唯一指纹使同一用户的重复上传返回同一 provider 记录。
- 文件先写专用 `CHAT_FILES` R2 bucket 的 `{uid}/{file_id}` 私有 key，再通过 direct OpenAI Files REST（文档 `purpose=assistants`、图片 `purpose=vision`）取得 provider id；API Core 不绑定该 bucket，也不复制 metadata writer。
- provider/R2/metadata 失败时标记 failed 并尽力清理对象/provider；账号删除会扫描并清除 D1 行及 `CHAT_FILES` 的 uid 前缀。
- GET 列表和 DELETE 都按 signed uid 隔离；跨账号 file id 返回 404，不暴露 R2 对象 key；仅在已认证的 metadata response 中返回 provider file id。

## 仍未完成

`/v1/files`、`/v2/files` 继续由 legacy owner 提供。旧 `FileChatTool` 还依赖 Firestore `users/{uid}/files`/chat session、GCS 缩略图、Pillow 和 OpenAI Assistants/vision 语义。配置 Cloudflare Images `IMAGES` binding 和 `CHAT_FILE_THUMBNAIL_SECRET` 时，Worker 会把图片转成 128px JPEG 写入私有 R2，并用短期 HMAC URL 提供读取；缺少任一能力时明确返回 `503 thumbnail_unavailable`。这只闭合上传/缩略图 authority，Assistants session continuity 和历史 Firestore/GCS backfill 仍需完成后才能切换两个 legacy path。

当前边界是同步的 Jobs provider admission，不是旧 API 的兼容 alias，也不宣称历史数据已迁移。`OPENAI_API_KEY` 需要以 Jobs Worker secret 注入 staging；缺失时请求 fail-closed。

本轮补上了最小的 D1 session attachment projection（migration `0111_chat_session_files.sql`），并由 API Core 提供显式 staging-only contract：

- `POST /v2/cf/chat-sessions/{session_id}/files` 只接受当前 uid 下 `cf_chat_files.status = 'ready'` 的 canonical `file_id`，重复绑定是幂等的；跨 uid、failed/deleted 或不存在的 id 统一返回 404。
- `GET /v2/cf/chat-sessions/{session_id}/files` 只读同一 uid/session 下仍为 ready 的文件，并返回安全的 FileChat metadata projection，不返回 R2 storage key。
- `DELETE /v2/cf/chat-sessions/{session_id}/files/{file_id}` 只解除 session 关联，不删除文件 authority；删 session、删文件和账号删除 residual sweep 都会清理关联。

这组 route 证明了 D1 attachment reader 的 uid/session/deletion fence，但尚未让 `/v2/messages` 消费附件；现阶段 API AI 仍对 `file_ids` 返回 `409 attachments_not_migrated`。因此 `/v1/files`、`/v2/files` 继续不切 owner。

## Legacy owner 切换门槛

当前不能仅把 Edge manifest 的 owner 改成 Jobs。旧上传接口的返回值会被后续桌面聊天请求继续使用，至少还需要以下闭合证据：

1. `cf_chat_files` 的 canonical row 必须有一个 Cloudflare chat-session reader。当前 API-AI 的 `/v2/messages` 对 `file_ids` 明确返回 `409 attachments_not_migrated`，因此上传成功后仍不能由 Cloudflare 聊天链路消费文件。
2. 非图片文件需要保留旧 Assistants `thread → message attachment → file_search → run` 的会话连续性；图片需要保留旧 vision 读取语义。当前 Jobs 只调用 OpenAI Files REST 取得 provider id，没有 Worker-side Assistants thread/assistant authority，也没有 D1 的 thread/file 关联投影。
3. 旧 Firestore `users/{uid}/files` 的历史 rows 以及其中的 `openai_file_id`、`thumb_name`、GCS thumbnail URL 必须先回放到 canonical D1/R2/provider 记录，并能验证重复上传、provider 删除和删号残留。`cf_chat_files` 目前只覆盖新上传 authority，不能让历史 id 在切换后凭空变成可读。
4. 兼容验证需要同时覆盖 legacy 多文件 200 response、session attachment 消费、图片缩略图 URL 过期/跨 uid 隔离，以及 provider/R2/D1 任一失败时的原子回滚。现有测试只证明 canonical `/v1/cf/chat-files` 和显式 opt-in alias 的上传/删除边界，不证明旧聊天 session parity。

因此，`LEGACY_CHAT_FILES_STAGING_ENABLED` 仍只能作为隔离 staging 的 opt-in 验证开关；它不是生产切换开关，也不应在缺少上述 reader、Assistants continuity 和历史回放证据时打开。完成门槛后应先用一批可删除的 Better Auth 账号做旧客户端回归，再同步更新 `backend-routes.json` 和 `routes.yaml` 的 owner。

## 代码证据

- Legacy upload 与持久化：`backend/routers/chat.py` 的 `/v1/files`、`/v2/files` 写本机临时文件，经 `FileChatTool.upload` 调用 Pillow/OpenAI Files，再把 FileChat rows 写入 Firestore chat database。
- Legacy 消费：`backend/routers/chat.py` 的 `/v2/messages` 将 `file_ids` 加入 Firestore chat session，随后由 `FileChatTool` 创建或恢复 OpenAI Assistants thread/assistant 并运行 file search；Cloudflare 当前 `deploy/cloudflare/python/api-ai/src/chat_generation_routes.py` 尚未承接这条附件分支。
- Cloudflare 新 authority：`deploy/cloudflare/workers/jobs/chat-file-routes.ts` 写 `cf_chat_files` 与专用 `CHAT_FILES` R2，并通过 direct OpenAI Files REST；`deploy/cloudflare/migrations/app/0111_chat_session_files.sql` 现在补上 D1 session attachment projection，但仍没有 legacy Assistants thread/assistant 关联或历史 backfill 状态。

## 最小闭合设计（projection 已实现，provider continuity 尚未实现）

若要推进两个 legacy upload owner，下一步最小数据契约应在 D1 增加 session attachment projection，而不是让聊天请求直接信任客户端传来的 provider id：

```sql
CREATE TABLE cf_chat_session_files (
  uid TEXT NOT NULL,
  session_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  attached_at INTEGER NOT NULL,
  source_message_id TEXT,
  PRIMARY KEY (uid, session_id, file_id),
  FOREIGN KEY (uid, file_id) REFERENCES cf_chat_files(uid, file_id)
);
```

该表已由 `0111_chat_session_files.sql` 创建，并带有复合主键、`cf_chat_sessions`/`cf_chat_files` 外键、uid mutation fence 和 account-deletion residual surface。`tests/test_chat_session_routes.py` 的 fixture 覆盖 ready-only、跨 uid/failed rejection、重复绑定、解除关联及安全 metadata projection；它不是 Assistants provider 的 wire fixture。

写入必须同时满足 uid、session uid 和 `cf_chat_files.status = 'ready'`，并在 `cf_chat_files` 删除/账号删号时级联或显式清理；读取必须按 uid+session_id 查询并限制数量，拒绝跨账号、failed/deleted 或不存在的 file id。`cf_chat_sessions` 还需要保存经迁移的 provider thread/assistant identity（或另一个 uid-scoped provider-session 表），并带 generation/deletion fence；否则新上传虽然有 provider id，旧会话仍无法恢复其 Assistants thread。

provider 侧至少需要在 Worker 可控超时内闭合 `thread retrieve/create`、`message attachment`、`run create/poll/list` 和 vision content 读取，并把 provider 状态、重试幂等键和不可恢复错误写入 D1。仅把 `cf_chat_files.provider_file_id` 传进 Workers AI，或把文件 bytes 拼进普通文本 prompt，都不等价于旧 Assistants `file_search` 语义。历史回放还需要 cursor/idempotency 表，能从 Firestore file row 和 provider object 恢复 canonical row；没有可读原始 provider object 的旧 row 必须明确标记不可迁移，不能生成伪造 file id。

在此设计落地前，当前最安全的可验证范围仍是 `/v1/cf/chat-files` 的上传/list/delete 和默认关闭的 staging aliases；不存在一个既有的 legacy GET/list/delete endpoint 可以独立切 owner 来绕过这条 session dependency。

## Assistants continuity adapter（显式 staging opt-in）

本轮补上了一个不影响 legacy owner 的 OpenAI Assistants continuity adapter。migration `0113_chat_assistant_provider.sql` 新增 `cf_chat_assistant_sessions`（D1 uid/session 到 OpenAI thread/assistant）和 `cf_chat_assistant_runs`（provider message/run、状态、幂等键、lease、错误及结果）。Jobs 提供三个显式 staging contract：

- `POST /v2/cf/chat-sessions/{session_id}/assistant-runs`：校验当前 Better Auth uid、D1 ready attachment projection 和 `Idempotency-Key`，通过 OpenAI Assistants v2 创建 thread/message/run，成功 admission 返回 `202`。
- `GET /v2/cf/chat-sessions/{session_id}/assistant-runs/{run_id}`：只读取同一 uid/session 的 run，并在 completed 时读取 assistant 文本结果。
- `DELETE /v2/cf/chat-sessions/{session_id}/assistant`：先删除 provider thread，再删除 D1 provider state；账号删除 residual sweep 同样覆盖两张表。

该 adapter 只有在 Jobs secrets `OPENAI_API_KEY`、`OPENAI_ASSISTANT_ID` 和 `CHAT_ASSISTANT_PROVIDER_STAGING_ENABLED=true` 同时配置时启用。provider REST 调用使用固定 Assistants v2 header、短重试预算和幂等键；Queue 的 `chat_assistant_poll` consumer 对 in-progress/transient 状态最多轮询 12 次，超出后把 D1 run 标记为 failed。队列 admission 失败不会丢失已创建的 provider run，客户端可用相同幂等键重试或直接 GET poll。

这仍然不是 `/v1/files`、`/v2/files` 的 owner 切换，也不是 `/v2/messages` 的附件消费实现。未覆盖的 legacy contract 包括 Firestore `users/{uid}/files`/chat session 历史回放、GCS thumbnail/provider object backfill、旧 `FileChatTool` 的完整多轮/SSE/response wire、Assistants 历史 thread 恢复、桌面客户端兼容回归和真实 staging provider secret/live probe。Workers AI 文本替代不作为等价实现；在这些证据补齐前，legacy aliases 继续默认 fail-closed。
