# Chat-file staging boundary

截至 2026-09-01，Cloudflare staging 的 canonical `/v1/cf/chat-files` 已由 Jobs
Worker 使用 R2 + D1 承载，文件问答默认由 Workers AI Queue 执行；不需要
`OPENAI_API_KEY`、`GEMINI_API_KEY` 或任何外部 AI provider secret。生产数据回填
和旧客户端 conformance 已按本期“空数据新部署”范围移出验收条件。

Edge 只在 Better Auth、account cutover、rate-limit 和 signed Jobs assertion
都通过后转发。Jobs 的 `CHAT_FILES_WORKERS_AI_ENABLED=true` 和
`CHAT_WORKERS_AI_ATTACHMENTS_ENABLED=true` 开启 Cloudflare-native 路径；
`LEGACY_CHAT_FILES_STAGING_ENABLED=false` 使旧 `/v1/files`、`/v2/files` aliases
明确返回 404，而不是回落本机/legacy。缺少 `CHAT_FILES` R2 或 Workers AI
binding 时返回明确 503；图片缩略图另需 Cloudflare Images binding 和 HMAC secret。

`0146_chat_files_workers_ai.sql` 允许 provider-neutral 文件 authority，
`0147_chat_runs_workers_ai.sql` 允许同一 D1 Queue run projection 由 Workers AI
执行。OpenAI Assistants continuity adapter 仍保留为显式、默认关闭的兼容代码，
不会被 canonical 路径调用。

## Session attachment staging evidence（2026-08-31）

远端 App D1 已应用 `0111_chat_session_files.sql`、`0146_chat_files_workers_ai.sql`
和 `0147_chat_runs_workers_ai.sql`。隔离 Better Auth 账号创建 chat session 返回
`200`；`GET /v2/cf/chat-sessions/{session_id}/files` 返回 `200 []`，不存在或跨账号
file id 的 attach/detach 返回 `404`。无外部 AI secret 时，canonical text upload
和 Workers AI attachment run 仍可工作；仅缺少 R2/AI binding 才返回 503。

这次闭合了 D1 的 uid/session/ready/deletion-fence reader 和 Workers AI run；
canonical response 的 `provider` 为 `cloudflare-workers-ai`，`openai_file_id` 为
`null`，不会向客户端伪造外部 provider id。

## 已闭合的 staging 子面

- Jobs Worker 校验 Better Auth signed context，并用 bounded multipart parser 限制 10 个文件、单文件 50 MiB、请求总量 100 MiB。
- D1 `cf_chat_files` 保存 uid、稳定请求指纹、provider-neutral 状态、大小、SHA-256 和私有对象 key；唯一指纹使同一用户的重复上传返回同一 canonical 记录。
- 文件写入专用 `CHAT_FILES` R2 bucket 的 `{uid}/{file_id}` 私有 key；文本/JSON/XML/YAML/CSV 内容在有界 Queue job 中读取并提交给 Workers AI，完全不经过 OpenAI/Gemini。
- R2/D1/Workers AI 失败时标记 failed 并由 Queue 重试；账号删除会扫描并清除 D1 行及 `CHAT_FILES` 的 uid 前缀。
- GET 列表和 DELETE 都按 signed uid 隔离；跨账号 file id 返回 404，不暴露 R2 对象 key；仅在已认证的 metadata response 中返回 provider file id。

## 当前限制（非本期阻塞）

旧 `/v1/files`、`/v2/files` aliases 已明确关闭（旧协议不在本期范围）。
canonical `/v1/cf/chat-files` 按 uid+内容指纹幂等写入 R2/D1；文本类附件由
Workers AI 读取并回答。配置 Cloudflare Images `IMAGES` binding 和
`CHAT_FILE_THUMBNAIL_SECRET` 时，图片会转成 128px JPEG 写入私有 R2，并用
短期 HMAC URL 提供读取；缺少任一能力时明确返回 `503 thumbnail_unavailable`。
图片问答和 PDF/二进制解析暂不在 native text slice 内，会返回有界的
`provider_rejected`，而不会偷偷调用 OpenAI/Gemini。

当前边界是 Jobs admission + Queue + Workers AI executor；不依赖外部 provider secret。
`OPENAI_API_KEY` 仅供显式、默认关闭的旧 Assistants 兼容 adapter，缺失时该分支
fail-closed，不影响 canonical 文件上传、文件问答或普通 Workers AI 聊天。

本轮补上了最小的 D1 session attachment projection（migration `0111_chat_session_files.sql`），并由 API Core 提供显式 staging-only contract：

- `POST /v2/cf/chat-sessions/{session_id}/files` 只接受当前 uid 下 `cf_chat_files.status = 'ready'` 的 canonical `file_id`，重复绑定是幂等的；跨 uid、failed/deleted 或不存在的 id 统一返回 404。
- `GET /v2/cf/chat-sessions/{session_id}/files` 只读同一 uid/session 下仍为 ready 的文件，并返回安全的 FileChat metadata projection，不返回 R2 storage key。
- `DELETE /v2/cf/chat-sessions/{session_id}/files/{file_id}` 只解除 session 关联，不删除文件 authority；删 session、删文件和账号删除 residual sweep 都会清理关联。

这组 route 证明了 D1 attachment reader 的 uid/session/deletion fence。带有 app/context
参数且含非空 `file_ids` 的 `/v2/messages` 现在由 Edge 路由到 Jobs Workers AI
attachment run；Jobs 按 app-scoped session 校验应用投影，文本内容在有界 prompt
中作为不可信参考数据注入。没有附件的 app/context 请求仍由 API-AI 处理。
新 Web 文件问答不需要任何 OpenAI/Gemini secret。

另外，API Core 的 staging-only `POST /v2/desktop/messages` 现在可以接收
`file_ids`：它只接受当前 uid 下 `cf_chat_files.status = 'ready'` 的 canonical
rows，在同一 D1 batch 中写入 `cf_chat_session_files`（带
`source_message_id`）并把安全 metadata projection 固化到该 message 的
`files_id`/`files` 字段。重复的 `client_message_id` 会沿用既有 payload hash
幂等语义；跨 uid、failed/deleted、不存在、重复或 assistant message 附件会在
任何 session/message mutation 前拒绝。这个 slice 让 persistence message reader
可回读文件 metadata，但不调用 Assistants、file_search 或任何 provider，所以
仍不等价于旧 `/v2/messages` 的附件聊天。

## 可选旧协议兼容窗口（不属于本期）

以下只描述未来若重新启用旧客户端兼容时的额外工作，不构成本期 Cloudflare
部署阻塞；本期明确不做旧协议切换：

1. 若未来需要旧客户端，才需要额外的 response/SSE parity；当前只接受 Cloudflare
   native response 和 202/poll contract。
2. 旧 Assistants `thread → file_search → run` 和 vision 语义不再作为目标；native
   文本问答使用 Workers AI，图片/PDF 等超出当前 bounded parser 的类型显式拒绝。
3. 旧 Firestore `users/{uid}/files` 的历史 rows、GCS thumbnail 和 provider object
   只在未来兼容窗口才需要审计；当前没有生产数据，因此不做回填。
4. 若未来启用旧 aliases，才需要 legacy 多文件 200 response 和旧客户端回归；
   当前 aliases 在 provider 调用前返回 404。

因此，`LEGACY_CHAT_FILES_STAGING_ENABLED=false` 是本期预期配置，而不是待修复
的 provider secret blocker。未来若重新打开旧 aliases，必须另行设计兼容窗口。

## 代码证据

- Legacy upload 与持久化：`backend/routers/chat.py` 的 `/v1/files`、`/v2/files` 写本机临时文件，经 `FileChatTool.upload` 调用 Pillow/OpenAI Files，再把 FileChat rows 写入 Firestore chat database。
- Legacy 消费：`backend/routers/chat.py` 的 `/v2/messages` 将 `file_ids` 加入 Firestore chat session，随后由 `FileChatTool` 创建或恢复 OpenAI Assistants thread/assistant 并运行 file search；Cloudflare exact `/v2/messages` 现在在显式 staging bridge 开启时把含附件的分支（包括 app/context）转到 Jobs，显式 `/v2/cf/chat-sessions/{session_id}/assistant-runs` 仍是底层 projection contract。
- Cloudflare 新 authority：`deploy/cloudflare/workers/jobs/chat-file-routes.ts` 写 `cf_chat_files` 与专用 `CHAT_FILES` R2；`0111`/`0118`/`0146`/`0147` 提供 session attachment、Workers AI run state 和 D1 message projection，不读取 Firestore/GCS，也不需要外部 AI secret。

## 历史回放与 legacy owner 切换设计

若要推进两个 legacy upload owner，历史回放和客户端切换仍必须复用已有 session attachment projection，而不是让聊天请求直接信任客户端传来的 provider id：

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

该表已由 `0111_chat_session_files.sql` 创建，并带有复合主键、`cf_chat_sessions`/`cf_chat_files` 外键、uid mutation fence 和 account-deletion residual surface。`tests/test_chat_session_routes.py` 的 fixture 覆盖 ready-only、跨 uid/failed rejection、重复绑定、解除关联及安全 metadata projection；`tests/chat-assistant-provider.test.ts` 另覆盖 Assistants provider 的 attachment wire 与 message projection。

写入必须同时满足 uid、session uid 和 `cf_chat_files.status = 'ready'`，并在 `cf_chat_files` 删除/账号删号时级联或显式清理；读取必须按 uid+session_id 查询并限制数量，拒绝跨账号、failed/deleted 或不存在的 file id。`cf_chat_sessions` 还需要保存经迁移的 provider thread/assistant identity（或另一个 uid-scoped provider-session 表），并带 generation/deletion fence；否则新上传虽然有 provider id，旧会话仍无法恢复其 Assistants thread。

provider 侧现在已在显式 staging adapter 中闭合 `thread create`、`message attachment`、`run create/poll/list` 和 vision/file-search content 读取，并把 provider 状态、重试幂等键、D1 message projection 和不可恢复错误写入 Cloudflare authority。仅把 `cf_chat_files.provider_file_id` 传进 Workers AI，或把 file bytes 拼进普通文本 prompt，都不等价于旧 Assistants `file_search` 语义。历史回放还需要 cursor/idempotency 表，能从 Firestore file row 和 provider object 恢复 canonical row；没有可读原始 provider object 的旧 row 必须明确标记不可迁移，不能生成伪造 file id。

当前可验证范围是 `/v1/cf/chat-files` 及 `/v1/files`、`/v2/files` aliases 的
上传/list/delete、session attachment reader、Assistants projection 和 staging
attachment bridge；bridge 的 app/context 分支会创建或复用对应 app-scoped session，
并在首次请求时幂等关联 ready file。旧客户端 conformance 属于后续兼容窗口，当前
新 Web/Worker 部署不依赖它。

## 旧 Assistants adapter（显式、默认关闭）

`0113_chat_assistant_provider.sql` 和 `0118_chat_assistant_message_projection.sql` 仍保留
旧 OpenAI Assistants continuity adapter 的 schema，供明确的历史兼容实验使用；它不是
canonical Cloudflare 路径的依赖。`0147_chat_runs_workers_ai.sql` 让相同 Queue
projection 可使用 `cloudflare-workers-ai` provider，而不创建外部 thread。

- `POST /v2/cf/chat-sessions/{session_id}/assistant-runs`：校验当前 Better Auth uid、D1 ready attachment projection 和 `Idempotency-Key`，默认创建 Workers AI Queue run，成功 admission 返回 `202`。
- `GET /v2/cf/chat-sessions/{session_id}/assistant-runs/{run_id}`：只读取同一 uid/session 的 run，并在 completed 时读取 assistant 文本结果。
- `DELETE /v2/cf/chat-sessions/{session_id}/assistant`：先删除 provider thread，再删除 D1 provider state；账号删除 residual sweep 同样覆盖两张表。

`0118_chat_assistant_message_projection.sql` 进一步把该显式 provider run 接回
Cloudflare 的 canonical chat history：admission 在 provider run 成功后写入 uid/session
绑定的 human message；Queue poll 在 provider 完成后以相同 run 幂等写入 assistant message，
并在 `cf_chat_assistant_message_projections` 中维护两侧状态。message JSON 只包含当前
D1 ready file 的安全 metadata（不会让客户端提供 provider id），session `message_count`
按 D1 message projection 重算，因此重复 Queue delivery 或 GET poll 不会重复计数。该
projection 与 run/session 一样有 account-deletion fence，并由 residual audit 覆盖。

Workers AI native branch 只要求 Jobs 的 `AI` binding 和 `CHAT_FILES` R2；文本内容在
Queue 中有界读取后调用 `env.AI.run`，结果写回 D1。旧 OpenAI Assistants branch
只有在 `CHAT_FILES_WORKERS_AI_ENABLED=false` 且 `CHAT_ASSISTANT_PROVIDER_STAGING_ENABLED=true`
时才会读取 `OPENAI_API_KEY`/`OPENAI_ASSISTANT_ID`，缺失时 fail-closed。

在显式 staging 开关打开时，Jobs exact aliases 已能返回 legacy `FileChat.model_dump()` 的六个
字段（`id`、`name`、`thumbnail`、`mime_type`、`openai_file_id`、`created_at`）；内部
`thumb_name` 不会越过 exact response boundary。开关关闭时 alias 在 provider 调用前返回
`404 legacy_route_disabled`，不会写入 `cf_chat_files`。这只是可回归的 response/gate
seam；在 staging route inventory 中已登记为 Jobs owner。它仍不是 production
parity 或历史数据切换证明。

这仍然不是旧客户端 wire parity。当前 exact `POST /v2/messages` 检出非空 `file_ids`
（包括 app/context）后，把同一份有界 JSON body 转发到 Jobs 的
`/v2/cf/messages/attachments`；无附件请求仍进入 API-AI 的 Workers AI text path。
Jobs bridge 复用 `cf_chat_session_files`、Workers AI run/message projection 和 Queue，
不读取或信任客户端 provider id；admission 成功返回 `202` JSON，并通过 `Location` 和
`x-omi-chat-stream: poll` 指向 run polling。旧 `envelope=openai` 与 Assistants SSE
只在显式兼容开关打开时存在，默认关闭且不影响 native path。

因此该 bridge 只是客户端迁移用的 staging contract，不是 `/v2/messages` legacy owner
切换。未覆盖的 legacy contract 包括 Firestore `users/{uid}/files`/chat session 历史
回放、GCS thumbnail/provider object backfill、旧 `FileChatTool` 的完整多轮/SSE/response
wire、Assistants 历史 thread 恢复、桌面客户端兼容回归和真实 staging provider
secret/live probe。Workers AI 文本替代不作为等价实现；在这些证据补齐前，legacy
aliases 继续默认 fail-closed。

## Attachment bridge contract（staging；envelope 默认关闭）

`POST /v2/messages` 的附件分支只识别 JSON body 中非空的 `file_ids`。Edge 重新读取一份
不超过 128 KiB 的 clone 来决定路由，原始 body 不会被 API-AI 分支提前消费；请求带
`app_id`、`plugin_id` 或 `context` 时同样转到 Jobs，Jobs 端再次以
128 KiB body、64 KiB text、20 个唯一 canonical file id、uid/session/`ready` 状态和
account-deletion fence 做校验，因此 Edge 路由判断不是安全边界。

Jobs admission 先复用同一 uid 下、同一 app scope 的 chat session 和 session-file projection，
再通过既有 Assistants thread/message/run adapter 创建 D1 projection。请求应带
`Idempotency-Key`（缺失时只生成一次性 key，不能依赖它做客户端重试）；首次 admission
返回 `202`，重复 key 返回已持久化的 `200` projection，Queue failure 明确返回 `503`
且不回落 legacy。响应不包含 provider credential，run 结果通过现有
`GET /v2/cf/chat-sessions/{sessionId}/assistant-runs/{runId}` 读取。

可选 envelope 只覆盖附件、纯文本、单次 bounded response：它没有 provider usage，
不会映射 tools/BYOK/multimodal/quota，也不提供旧 `/v2/messages` 的完整多轮 SSE、
同步 response、Firestore session 连续性或历史 file backfill，因此不能作为 legacy
owner 切换证据。`/v2/chat/completions` 仍仅在 body 含顶层 `file_ids`、无 app/plugin/
context 且满足上述 gate 时进入该 adapter；普通 text completions 继续 fail-closed。
验证命令：

```bash
npm test -- --run tests/chat-attachments-bridge.test.ts tests/edge.test.ts
npm run typecheck
npm run validate:manifest
```

## Historical Firestore/GCS reconciliation planner（默认 dry-run）

本轮新增 `scripts/chat-file-reconcile.mjs` 和 migration
`0119_chat_file_import_ledger.sql`，用于把 Firestore FileChat metadata 与旧 GCS
object 位置整理成可审计的 D1/R2 copy plan。它只接受有界 JSON export（最多 5,000
行、8 MiB），要求无凭据的 `gs://bucket/object` URI、SHA-256、正数且不超过 50 MiB
的 size、合法 OpenAI `file-*` provider id，并按
`sha256(uid + source_file_id + checksum)` 生成稳定 `import_id`。同一输入重复出现
只会生成一个 plan；同一 ledger key 的不同 plan hash 会被标成
`conflicting_duplicate_plan`，不会覆盖原 metadata；同一 uid 的 destination key 或
不同 uid 间重复的 provider file id 也会在生成 SQL 前被标成冲突，不让 D1 唯一约束把
整批导入变成部分执行。

命令默认只输出 JSON、ledger SQL 和 R2 copy plan，不连接 Firestore、GCS、D1 或 R2，
也不会把任何 row 写成 `cf_chat_files.status = 'ready'`：

```bash
node deploy/cloudflare/scripts/chat-file-reconcile.mjs \
  --input /path/to/firestore-chat-files.json \
  [--fenced-uid <uid>]
```

缺少 checksum/provider id、非法 MIME/size、已进入账号删除 fence 的 uid 会生成
`blocked` ledger entry，且不会生成 R2 copy step；`0119` ledger 本身有 insert/update
deletion-fence trigger，并纳入 account-deletion residual scan。该 planner 不能代替
线上删号状态读取，也没有执行远端 GCS copy/provider revalidation 的 apply 模式；未来
要真正导入，必须在另一个显式 gated executor 中先再次检查 D1 fence、读取并校验 GCS
bytes checksum、确认 provider id 属于该 uid，再原子提交 R2 object 与 canonical
`cf_chat_files` row。历史 Firestore/GCS 回放和旧 `/v1/files`、`/v2/files` owner 切换
仍未完成。

## Historical chat session/message replay planner（reviewed apply/verify）

为补上 session continuity 的最小可执行面，本轮增加
`scripts/chat-history-reconcile.mjs` 与 migration
`0128_chat_history_reconciliation.sql`。它只接收已经去敏的 Firestore 导出清单，
覆盖 `users/{uid}/chat_sessions` 和 `users/{uid}/messages`；输入必须提供 Cloudflare
目标 uid、目标 `account_generation`、来源账号指纹和导出 SHA-256。脚本会拒绝凭据、
Firebase uid/token、OpenAI provider credential 等字段，限制清单 8 MiB/5,000 个实体，
校验 session/message 归属、`message_count`、app id 和消息结构。带有 `files_id` 的消息
会被整体阻塞，直到独立的 chat-file 回放先验证出 canonical `cf_chat_files` rows，
不会制造指向不存在文件的历史消息。

命令输出 reviewed plan、apply SQL 和 verify SQL，不连接 Firestore、GCS、D1 或 provider：

```bash
npm run chat:reconcile -- --input /path/to/chat-history-manifest.json \
  [--fenced-uid <uid>]
```

apply SQL 只对 `cf_account_cutover` 中同时满足 `state='new'`、
`checkpoint_phase='completed'`、`destination_backend_bound=1` 且 generation 完全相同的
账号生效，并再次检查 deletion intent/tombstone。session/message 目标行带有来源行指纹、
导出指纹和 generation 标记；插入使用 `ON CONFLICT DO NOTHING`，已有目标行不会被覆盖。
ledger 以 uid+entity 的唯一键阻止同一历史实体被不同导出静默替换；重复执行同一 plan
保持幂等。apply 后执行输出的 verify SQL，必须返回零行；它会同时发现 ledger 未完成、
目标 marker 缺失/不一致和 generation 冲突。

这是可审计的历史文本 session/message 回放切片，不是自动远端导入，也不等价于旧
Assistants thread/file_search、GCS thumbnail/provider object 或 `/v2/messages` 的完整
wire parity。真实 Firestore export、账号 cutover marker 和独立文件/provider 验证完成前，
不得把该 planner 视作 production owner switch 证据。

### Firestore export verification tool（默认 dry-run）

如果手头是 Firestore chat history 的 JSON export bytes，可先用独立校验器按原始字节计算
SHA-256，再交给同一个 `planChatHistoryReconciliation` planner。它只读 bounded UTF-8
bytes，不连接 Firestore、GCS、D1 或任何 provider，也不会把凭据写入输出：

```bash
npm run chat:export-verify -- \
  --export /path/to/chat-history-export.json \
  --expected-sha256 <sha256>
```

只有显式指定 `--apply <https://jobs.example/internal/chat-history/apply>` 时才会向现有
Jobs endpoint 发起一次 apply 请求；命令行同时要求 `--expected-sha256`、`ADMIN_KEY`
和至少 32 字节的 `CHAT_HISTORY_IMPORT_SIGNING_SECRET`（可用
`--admin-key-env`/`--signing-secret-env` 改变环境变量名）。脚本在内存中生成
HMAC-SHA256，签名覆盖 canonical 排序后的 `batch_id`、manifest 和 entry metadata，
secret 不进入 plan、HTTP body 或 JSON 输出。apply 每批最多 20 个已 stage 实体，默认行为
始终是 dry-run。

### Reviewed apply executor（默认关闭）

为避免把“生成 SQL”误当成已经落库，Jobs Worker 另提供一个仅供受控操作员使用的
`POST /internal/chat-history/apply`。它只在
`CHAT_HISTORY_IMPORT_STAGING_ENABLED=true` 且请求带 `secret-key: ADMIN_KEY` 时启用；
请求体必须是上面 planner 的 reviewed JSON（可使用 `manifestHash`/`batch_id`），并以
`CHAT_HISTORY_IMPORT_SIGNING_SECRET`（至少 32 字节）的 HMAC-SHA256 对 canonical JSON
signature payload 生成 URL-safe Base64 签名，放入
`x-chat-history-plan-signature`。payload 覆盖排序后的 batch、manifest 以及每个 entry 的
uid/entity/generation/source/import/plan hash 元数据；body 上限 1 MiB、每批最多 20 个实体，Worker 会再次
校验来源集合、行字段、所有 SHA-256、session/message 结构和 source-row hash，不接受
凭据、Firebase/Provider token、邮箱或文件引用。

Migration `0130_chat_history_apply_receipts.sql` 保存每个 `(uid, import_id)` 的内容绑定
receipt。apply 在一个 D1 batch 中先写 `cf_chat_history_import_ledger`，按 session→message
顺序写带来源 marker 的 canonical rows，再 finalize ledger 并写 receipt；已有相同 receipt
只返回幂等 replay 结果，不覆盖目标行。每次 apply 都要求 account cutover 已完成且 generation
完全匹配，并检查 deletion intent/tombstone；receipt、ledger 和目标 marker 还有 D1 trigger
作第二道 fence。

该 endpoint 是审阅后的 staging apply seam，不会读取 Firestore/GCS，也不会自动发现或执行
导出文件；当前 gate 默认关闭，尚未做真实历史 export、远端正向 apply 或生产 owner cutover。
启用前必须人工审阅 planner 输出并先完成独立 chat-file/provider continuity 验证。

### Chat-file history reviewed apply（Jobs-direct）

历史文件的 reviewed apply endpoint 只注册在 Jobs Worker 的内部面，故意不进入
`routes.yaml` 或 Edge proxy。它不是面向客户端的 owner alias；调用方必须在受控 Jobs
网络边界内提供 operator key、content-bound plan 和 provider attestation。即使 gate
打开，executor 也会在写入 canonical `cf_chat_files.status='ready'` 前重新执行
`CHAT_FILES.head()` 的 size/checksum、provider HMAC、account-generation、cutover 与
deletion-fence 检查；缺任一条件只返回稳定错误，不会创建 ready row。若未来需要从 Edge
调用，必须另行加入受保护的 manifest route、签名转发和 live negative/positive probe，不能
把 Jobs-direct endpoint 视为已经完成 Edge owner 切换。
