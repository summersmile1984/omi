# Chat-file staging boundary

截至 2026-09-01，Cloudflare staging 已将 `/v1/files` 与 `/v2/files` 两个
exact upload 入口交给 Jobs Worker；canonical `/v1/cf/chat-files` 仍保留供
迁移客户端使用。生产切换仍需历史回放和旧客户端 conformance 证据。

Edge 只在 Better Auth、account cutover、rate-limit 和 signed Jobs assertion
都通过后转发；Jobs 的 `LEGACY_CHAT_FILES_STAGING_ENABLED=true` 是 staging
owner 开关。缺少 OpenAI Files secret、CHAT_FILES R2 或 Images thumbnail 能力
时返回明确 `503`，不会回落本机/legacy 上传。

本轮发布同时应用了 `0113_chat_assistant_provider.sql` 与 `0114_gemini_proxy.sql`，API-AI `15a48911-bc8a-41f1-bd40-7a671f5d24e5`、Jobs `969c6d84-1cd3-4c8f-8781-914c44366349`、Edge `261cf422-4a28-4841-b316-cea5711f9d7a` 已上线。Assistant continuity adapter 仍由 `CHAT_ASSISTANT_PROVIDER_STAGING_ENABLED` 显式开启；当前未配置 OpenAI provider secret，临时 Better Auth 账号命中关闭开关时返回 `404 legacy_route_disabled`，因此不代表 legacy owner 已切换。

## Session attachment staging evidence（2026-08-31）

远端 App D1 已应用 `0111_chat_session_files.sql`。API Core `c4305fe3-aea6-450d-ac52-b0be2fcd340c`、Jobs `a5d71c63-8c1f-4b68-a614-5f344b785bba`、Edge `02680195-2ab7-43fe-ae8f-de93ef354ffd` 已发布。隔离 Better Auth 账号创建 chat session 返回 `200`；`GET /v2/cf/chat-sessions/{session_id}/files` 返回 `200 []`，不存在或跨账号 file id 的 attach/detach 返回 `404`，未配置 OpenAI Files provider 时上传返回 `503 provider_unavailable`。公开删号请求返回 `200`，session-file 关联没有残留；本次即时检查仍看到 deletion intent，最终 tombstone 交由异步 residual sweep 完成。

这次只闭合 D1 的 uid/session/ready/deletion-fence reader，不代表历史
Firestore/GCS 回放已完成；Assistants thread/file_search/run 由已有显式
`/v2/cf/chat-sessions/.../assistant-runs` seam 提供，exact upload 不会把
provider id 交给客户端。

## 已闭合的 staging 子面

- Jobs Worker 校验 Better Auth signed context，并用 bounded multipart parser 限制 10 个文件、单文件 50 MiB、请求总量 100 MiB。
- D1 `cf_chat_files` 保存 uid、稳定请求指纹、provider file id、大小、SHA-256、状态和私有对象 key；唯一指纹使同一用户的重复上传返回同一 provider 记录。
- 文件先写专用 `CHAT_FILES` R2 bucket 的 `{uid}/{file_id}` 私有 key，再通过 direct OpenAI Files REST（文档 `purpose=assistants`、图片 `purpose=vision`）取得 provider id；API Core 不绑定该 bucket，也不复制 metadata writer。
- provider/R2/metadata 失败时标记 failed 并尽力清理对象/provider；账号删除会扫描并清除 D1 行及 `CHAT_FILES` 的 uid 前缀。
- GET 列表和 DELETE 都按 signed uid 隔离；跨账号 file id 返回 404，不暴露 R2 对象 key；仅在已认证的 metadata response 中返回 provider file id。

## 仍未完成

`/v1/files`、`/v2/files` 的 staging owner 已调用同一个 canonical handler：
旧的六字段列表 response（`id`、`name`、`thumbnail`、`mime_type`、
`openai_file_id`、`created_at`）保持不变；重复请求按 uid+内容指纹返回同一个
row。旧 `FileChatTool` 的历史 Firestore `users/{uid}/files`、GCS 缩略图和
Pillow 不能直接在 Worker 重放。配置 Cloudflare Images `IMAGES` binding 和
`CHAT_FILE_THUMBNAIL_SECRET` 时，图片会转成 128px JPEG 写入私有 R2，并用
短期 HMAC URL 提供读取；缺少任一能力时明确返回 `503 thumbnail_unavailable`。
当前闭合的是新上传 authority，不是历史 backfill 或完整 Assistants session
parity。

当前边界是同步的 Jobs provider admission，不是旧 API 的兼容 alias，也不宣称历史数据已迁移。`OPENAI_API_KEY` 需要以 Jobs Worker secret 注入 staging；缺失时请求 fail-closed。

本轮补上了最小的 D1 session attachment projection（migration `0111_chat_session_files.sql`），并由 API Core 提供显式 staging-only contract：

- `POST /v2/cf/chat-sessions/{session_id}/files` 只接受当前 uid 下 `cf_chat_files.status = 'ready'` 的 canonical `file_id`，重复绑定是幂等的；跨 uid、failed/deleted 或不存在的 id 统一返回 404。
- `GET /v2/cf/chat-sessions/{session_id}/files` 只读同一 uid/session 下仍为 ready 的文件，并返回安全的 FileChat metadata projection，不返回 R2 storage key。
- `DELETE /v2/cf/chat-sessions/{session_id}/files/{file_id}` 只解除 session 关联，不删除文件 authority；删 session、删文件和账号删除 residual sweep 都会清理关联。

这组 route 证明了 D1 attachment reader 的 uid/session/deletion fence。带有旧 app/context
参数的 `/v2/messages` 仍在 API-AI 返回 `409`；不带这些参数且含非空 `file_ids` 的请求
现在只在 Edge 的显式 staging bridge 开关打开后才进入 Jobs Assistants adapter。因此
exact `/v1/files`、`/v2/files` 已在 staging 切到 Jobs owner；`/v2/messages` 不
随上传入口一起切换。

另外，API Core 的 staging-only `POST /v2/desktop/messages` 现在可以接收
`file_ids`：它只接受当前 uid 下 `cf_chat_files.status = 'ready'` 的 canonical
rows，在同一 D1 batch 中写入 `cf_chat_session_files`（带
`source_message_id`）并把安全 metadata projection 固化到该 message 的
`files_id`/`files` 字段。重复的 `client_message_id` 会沿用既有 payload hash
幂等语义；跨 uid、failed/deleted、不存在、重复或 assistant message 附件会在
任何 session/message mutation 前拒绝。这个 slice 让 persistence message reader
可回读文件 metadata，但不调用 Assistants、file_search 或任何 provider，所以
仍不等价于旧 `/v2/messages` 的附件聊天。

## Legacy owner 切换门槛

当前不能据此宣称 production parity。旧上传接口的返回值会被后续桌面聊天请求继续使用，至少还需要以下闭合证据：

1. `cf_chat_files` 的 canonical row 必须有一个 Cloudflare chat-session reader。该 reader
   已由 `/v2/cf/chat-sessions/{session_id}/files` 提供；exact `/v2/messages` 的附件
   bridge 目前只是默认关闭的 202/poll staging contract，仍不具备旧 response/SSE
   parity，因此上传成功后仍不能把 exact legacy chat owner 切到 Cloudflare。
2. 非图片文件需要保留旧 Assistants `thread → message attachment → file_search → run` 的会话连续性；图片需要保留旧 vision 读取语义。显式 staging adapter 已闭合新 run 的 Worker-side Assistants thread/assistant authority 和 D1 thread/file/message projection，但 exact legacy `/v2/messages` 仍未接入该 adapter。
3. 旧 Firestore `users/{uid}/files` 的历史 rows 以及其中的 `openai_file_id`、`thumb_name`、GCS thumbnail URL 必须先回放到 canonical D1/R2/provider 记录，并能验证重复上传、provider 删除和删号残留。`cf_chat_files` 目前只覆盖新上传 authority，不能让历史 id 在切换后凭空变成可读。
4. 兼容验证需要同时覆盖 legacy 多文件 200 response、session attachment 消费、图片缩略图 URL 过期/跨 uid 隔离，以及 provider/R2/D1 任一失败时的原子回滚。现有测试覆盖 canonical Files、session attachment 和显式 Assistants adapter 的 provider/message projection，但不证明旧客户端 response/SSE/session parity。

因此，`LEGACY_CHAT_FILES_STAGING_ENABLED` 当前是隔离 staging owner 开关，不是
production cutover 证明。完成门槛后仍应先用一批可删除的 Better Auth 账号做旧
客户端回归，再执行历史 Firestore/GCS/provider object backfill、残留扫描，并同步
更新 production rollout policy。

## 代码证据

- Legacy upload 与持久化：`backend/routers/chat.py` 的 `/v1/files`、`/v2/files` 写本机临时文件，经 `FileChatTool.upload` 调用 Pillow/OpenAI Files，再把 FileChat rows 写入 Firestore chat database。
- Legacy 消费：`backend/routers/chat.py` 的 `/v2/messages` 将 `file_ids` 加入 Firestore chat session，随后由 `FileChatTool` 创建或恢复 OpenAI Assistants thread/assistant 并运行 file search；Cloudflare exact `/v2/messages` 现在只在显式 staging bridge 开启时把无 app/context 的附件分支转到 Jobs，显式 `/v2/cf/chat-sessions/{session_id}/assistant-runs` 仍是底层 projection contract。
- Cloudflare 新 authority：`deploy/cloudflare/workers/jobs/chat-file-routes.ts` 写 `cf_chat_files` 与专用 `CHAT_FILES` R2，并通过 direct OpenAI Files REST；`0111`/`0113`/`0118` 共同提供 session attachment、Assistants provider state 和 D1 message projection，但仍没有历史 Firestore/GCS backfill。

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

在历史回放和旧客户端 conformance 完成前，当前可验证范围是 `/v1/cf/chat-files`
及 `/v1/files`、`/v2/files` aliases 的上传/list/delete、session attachment reader、
Assistants projection 和默认关闭的 attachment bridge；exact upload owner 的
staging 切换不绕过 session dependency。

## Assistants continuity adapter（显式 staging opt-in）

本轮补上了一个不影响 legacy owner 的 OpenAI Assistants continuity adapter。migration `0113_chat_assistant_provider.sql` 新增 `cf_chat_assistant_sessions`（D1 uid/session 到 OpenAI thread/assistant）和 `cf_chat_assistant_runs`（provider message/run、状态、幂等键、lease、错误及结果）。Jobs 提供三个显式 staging contract：

- `POST /v2/cf/chat-sessions/{session_id}/assistant-runs`：校验当前 Better Auth uid、D1 ready attachment projection 和 `Idempotency-Key`，通过 OpenAI Assistants v2 创建 thread/message/run，成功 admission 返回 `202`。
- `GET /v2/cf/chat-sessions/{session_id}/assistant-runs/{run_id}`：只读取同一 uid/session 的 run，并在 completed 时读取 assistant 文本结果。
- `DELETE /v2/cf/chat-sessions/{session_id}/assistant`：先删除 provider thread，再删除 D1 provider state；账号删除 residual sweep 同样覆盖两张表。

`0118_chat_assistant_message_projection.sql` 进一步把该显式 provider run 接回
Cloudflare 的 canonical chat history：admission 在 provider run 成功后写入 uid/session
绑定的 human message；Queue poll 在 provider 完成后以相同 run 幂等写入 assistant message，
并在 `cf_chat_assistant_message_projections` 中维护两侧状态。message JSON 只包含当前
D1 ready file 的安全 metadata（不会让客户端提供 provider id），session `message_count`
按 D1 message projection 重算，因此重复 Queue delivery 或 GET poll 不会重复计数。该
projection 与 run/session 一样有 account-deletion fence，并由 residual audit 覆盖。

该 adapter 只有在 Jobs secrets `OPENAI_API_KEY`、`OPENAI_ASSISTANT_ID` 和 `CHAT_ASSISTANT_PROVIDER_STAGING_ENABLED=true` 同时配置时启用。provider REST 调用使用固定 Assistants v2 header、短重试预算和幂等键；Queue 的 `chat_assistant_poll` consumer 对 in-progress/transient 状态最多轮询 12 次，超出后把 D1 run 标记为 failed。队列 admission 失败不会丢失已创建的 provider run，客户端可用相同幂等键重试或直接 GET poll。

在显式 staging 开关打开时，Jobs exact aliases 已能返回 legacy `FileChat.model_dump()` 的六个
字段（`id`、`name`、`thumbnail`、`mime_type`、`openai_file_id`、`created_at`）；内部
`thumb_name` 不会越过 exact response boundary。开关关闭时 alias 在 provider 调用前返回
`404 legacy_route_disabled`，不会写入 `cf_chat_files`。这只是可回归的 response/gate
seam；在 staging route inventory 中已登记为 Jobs owner。它仍不是 production
parity 或历史数据切换证明。

这仍然不是 exact `/v2/messages` 的 legacy wire parity。当前只新增了一个默认关闭
的附件 bridge：Edge 会在 exact
`POST /v2/messages` 检出非空 `file_ids`（且没有旧 app/context 参数）后，把同一份
有界 JSON body 转发到 Jobs 的 `/v2/cf/messages/attachments`；无附件请求仍原样进入
API-AI 的 Workers AI text path。Jobs bridge 复用 `cf_chat_session_files`、Assistants
run/message projection 和 Queue，不读取或信任客户端 provider id；admission 成功返回
`202` JSON，并通过 `Location` 和 `x-omi-chat-stream: poll` 指向 run polling，而不是旧
客户端的 SSE。另有独立的 `CHAT_ATTACHMENT_ENVELOPE_STAGING_ENABLED` 开关：打开后，
`envelope=messages` 只在 Assistants run 已完成且 D1 message projection ready 时返回旧
`data: ...`/`done: <base64 ResponseMessage>` SSE；`envelope=openai` 对简单字符串
`messages` 返回 OpenAI sync JSON 或 `stream=true` SSE。任何等待超时仍返回 202/poll，
不会伪造完成结果。两个开关（`CHAT_ASSISTANT_PROVIDER_STAGING_ENABLED` 和
`CHAT_ATTACHMENT_ENVELOPE_STAGING_ENABLED`）都必须显式打开，关闭时 bridge 返回
`404 legacy_route_disabled`。

因此该 bridge 只是客户端迁移用的 staging contract，不是 `/v2/messages` legacy owner
切换。未覆盖的 legacy contract 包括 Firestore `users/{uid}/files`/chat session 历史
回放、GCS thumbnail/provider object backfill、旧 `FileChatTool` 的完整多轮/SSE/response
wire、Assistants 历史 thread 恢复、桌面客户端兼容回归和真实 staging provider
secret/live probe。Workers AI 文本替代不作为等价实现；在这些证据补齐前，legacy
aliases 继续默认 fail-closed。

## Attachment bridge contract（默认关闭）

`POST /v2/messages` 的附件分支只识别 JSON body 中非空的 `file_ids`。Edge 重新读取一份
不超过 128 KiB 的 clone 来决定路由，原始 body 不会被 API-AI 分支提前消费；请求带
`app_id`、`plugin_id` 或 `context` 时继续留在 API-AI 的现有拒绝边界。Jobs 端再次以
128 KiB body、64 KiB text、20 个唯一 canonical file id、uid/session/`ready` 状态和
account-deletion fence 做校验，因此 Edge 路由判断不是安全边界。

Jobs admission 先复用同一 uid 下已有的非 app chat session 和 session-file projection，
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
