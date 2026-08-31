# Chat-file staging boundary

截至 2026-08-31，Cloudflare 只承接显式的 staging-only `/v1/cf/chat-files`，并没有切换 legacy `/v1/files` 或 `/v2/files`。

## 已闭合的 staging 子面

- Jobs Worker 校验 Better Auth signed context，并用 bounded multipart parser 限制 10 个文件、单文件 50 MiB、请求总量 100 MiB。
- D1 `cf_chat_files` 保存 uid、稳定请求指纹、provider file id、大小、SHA-256、状态和私有对象 key；唯一指纹使同一用户的重复上传返回同一 provider 记录。
- 文件先写专用 `CHAT_FILES` R2 bucket 的 `{uid}/{file_id}` 私有 key，再通过 direct OpenAI Files REST（文档 `purpose=assistants`、图片 `purpose=vision`）取得 provider id；API Core 不绑定该 bucket，也不复制 metadata writer。
- provider/R2/metadata 失败时标记 failed 并尽力清理对象/provider；账号删除会扫描并清除 D1 行及 `CHAT_FILES` 的 uid 前缀。
- GET 列表和 DELETE 都按 signed uid 隔离；跨账号 file id 返回 404，不暴露对象或 provider id。

## 仍未完成

`/v1/files`、`/v2/files` 继续由 legacy owner 提供。旧 `FileChatTool` 还依赖 Firestore `users/{uid}/files`/chat session、GCS 缩略图、Pillow 和 OpenAI Assistants/vision 语义。配置 Cloudflare Images `IMAGES` binding 和 `CHAT_FILE_THUMBNAIL_SECRET` 时，Worker 会把图片转成 128px JPEG 写入私有 R2，并用短期 HMAC URL 提供读取；缺少任一能力时明确返回 `503 thumbnail_unavailable`。这只闭合上传/缩略图 authority，Assistants session continuity 和历史 Firestore/GCS backfill 仍需完成后才能切换两个 legacy path。

当前边界是同步的 Jobs provider admission，不是旧 API 的兼容 alias，也不宣称历史数据已迁移。`OPENAI_API_KEY` 需要以 Jobs Worker secret 注入 staging；缺失时请求 fail-closed。

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
- Cloudflare 新 authority：`deploy/cloudflare/workers/jobs/chat-file-routes.ts` 只写 `cf_chat_files` 与专用 `CHAT_FILES` R2，并通过 direct OpenAI Files REST；`deploy/cloudflare/migrations/app/0101_chat_files.sql` / `0106_chat_file_thumbnails.sql` 没有 legacy session/thread 关联或历史 backfill 状态。
