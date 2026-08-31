# Chat-file staging boundary

截至 2026-08-31，Cloudflare 只承接显式的 staging-only `/v1/cf/chat-files`，并没有切换 legacy `/v1/files` 或 `/v2/files`。

## 已闭合的 staging 子面

- Jobs Worker 校验 Better Auth signed context，并用 bounded multipart parser 限制 10 个文件、单文件 50 MiB、请求总量 100 MiB。
- D1 `cf_chat_files` 保存 uid、稳定请求指纹、provider file id、大小、SHA-256、状态和私有对象 key；唯一指纹使同一用户的重复上传返回同一 provider 记录。
- 文件先写专用 `CHAT_FILES` R2 bucket 的 `{uid}/{file_id}` 私有 key，再通过 direct OpenAI Files REST（`purpose=assistants`）取得 provider id；API Core 不绑定该 bucket，也不复制 metadata writer。
- provider/R2/metadata 失败时标记 failed 并尽力清理对象/provider；账号删除会扫描并清除 D1 行及 `CHAT_FILES` 的 uid 前缀。
- GET 列表和 DELETE 都按 signed uid 隔离；跨账号 file id 返回 404，不暴露对象或 provider id。

## 仍未完成

`/v1/files`、`/v2/files` 继续由 legacy owner 提供。旧 `FileChatTool` 还依赖 Firestore `users/{uid}/files`/chat session、GCS 缩略图、Pillow 和 OpenAI Assistants/vision 语义。当前 Worker 只接受非图片文件；图片返回 `thumbnail_unavailable`，直到 Worker-safe thumbnail contract、Assistants session continuity 和历史 Firestore/GCS backfill 都有可验证的 authority/迁移方案后，才可切换两个 legacy path。

当前边界是同步的 Jobs provider admission，不是旧 API 的兼容 alias，也不宣称历史数据已迁移。`OPENAI_API_KEY` 需要以 Jobs Worker secret 注入 staging；缺失时请求 fail-closed。
