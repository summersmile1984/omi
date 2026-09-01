# Data-protection migration gate

截至 2026-09-01，`/v1/users/migration/requests`、`/batch-requests` 和
`/requests/data-protection-level/finalize` 只由 API Core 提供一个 D1-backed
fail-closed boundary。它们不会调用 legacy，也不会写入伪造的成功 receipt。

## 已确认的 legacy 加密契约

`backend/utils/encryption.py` 的增强保护格式是：

- master secret 是 UTF-8 bytes（当前实现要求至少 32 bytes）；
- HKDF-SHA256，`salt = uid.encode("utf-8")`，`info =
  b"user-data-encryption"`，输出 32 bytes；
- AES-256-GCM，随机 12-byte nonce；存储值是标准 Base64 编码的
  `nonce || ciphertext || tag`；
- memory 至少保护 `content` 和 JSON 编码后的 `evidence`；conversation
  保护 `transcript_segments`（可能先压缩）及 photos 子集合的 `base64`；
  chat 保护 message 的 `text`。

迁移 executor 不能把解密失败当成 plaintext。legacy `decrypt` 为了避免
数据丢失会原样返回输入值，但迁移必须把认证失败视为 terminal error，避免
把 opaque ciphertext 再次加密。

## 当前 D1 缺口

`0103_data_protection_migration.sql` 只有 capability control 和
`cf_data_protection_migration_runs` receipt/lease 草案。`0137` 现在补充了
投影 source 的 revision marker，以及 conversation/chat 的 protection-level
marker；它仍然不把既有 plaintext 当成 ciphertext：

- `cf_memories.content` 和已投影的 `evidence_json` 仍是 plaintext；
- `cf_conversations` 的
  `structured_json`、`transcript_segments_json` 和 FTS searchable text
  没有加密/清除契约；新增的 marker 不能替代该契约；
- `cf_chat_messages` 的 `message_json` 内嵌 `text` 没有可区分的 encrypted
  envelope；新增的 marker 不能替代该契约；
- API Core、Jobs、MCP、persona、knowledge-graph、integration 和 FTS
  reader 仍直接把这些列作为 JSON/plaintext 读取。只改写列值会破坏读取，
  保留旧 plaintext 又不能满足 enhanced at-rest protection；
- Worker Jobs 尚无专用 data-protection key binding、source revision/CAS
  和可证明的全量 derived-content（尤其 FTS）处理契约。

因此即使 operator 手工把 control row 标记为 `enabled=1, executor_state=ready`，
三个 mutation endpoint 仍必须返回 `503 encryption_executor_unavailable`，
不创建 run、不更新 source row。行为 guard 位于
`deploy/cloudflare/python/api-core/tests/test_migration_routes.py`，覆盖
single、batch、finalize 以及 plaintext 不变性。

## 已落地的 staging preparation executor

`deploy/cloudflare/workers/jobs/data-protection-executor.ts` 提供了一个更窄的
内部 staging seam：`POST/GET /internal/data-protection/migrations*` 只接受
admin key，并且要求 `DATA_PROTECTION_EXECUTOR_STAGING_ENABLED=true` 与至少
32-byte `DATA_PROTECTION_ENCRYPTION_SECRET`。它读取已经投影到 D1 的
memory/conversation/chat source，绑定完整 source hash 后写入已有
`cf_data_protection_migration_runs`，通过 `JOBS` queue 异步处理；consumer
使用 lease、幂等 request fingerprint、source-drift 检测、重试和 account
deletion fence。

executor 生成的只是 `result_json` 中的 legacy-compatible
HKDF/AES-GCM ciphertext preparation artifact（scheme 与
`backend/utils/encryption.py` 相同），不会改写 canonical D1 rows、FTS 或
任何 reader。因此它证明了“投影数据可被安全准备”的最小闭环，但不能被
解释为历史数据迁移、enhanced at-rest cutover 或 `/v1/users/migration/*`
owner 切换。当前 gate 默认关闭，且 source revision drift、缺字段、无 key
都会 fail closed。

## 已落地的纯契约 slice

`deploy/cloudflare/workers/shared/legacy-data-protection.ts` 现在提供一个未
接入 route/reader 的 Workers Web Crypto 契约实现：按上述 uid-salted HKDF
派生 AES-256-GCM key，严格校验 Python 使用的标准 Base64、12-byte nonce 和
16-byte GCM tag，并在认证或 UTF-8 解码失败时抛出 terminal error。它不会
把输入 ciphertext 当 plaintext 返回；只有 legacy 对空 optional 字段的表示
`""` 被显式保留为空值。`deploy/cloudflare/tests/legacy-data-protection.test.ts`
包含 Python 固定 nonce fixture、随机加密 round-trip、篡改/截断/URL-safe
Base64 拒绝和无效 secret/uid 测试。

这个 slice 只证明跨运行时的 envelope/失败语义；连同 preparation executor
也不提供 canonical source replay、reader 适配或 cutover 能力。因此在所有
reader/derived index 和 canonical write contract 闭合前，仍不可启用数据
保护迁移控制行或切换 legacy route owner。

## 开放 executor 前置条件

要把上述准备 artifact 进一步变成正式迁移，还需要一个独立可审阅的
migration slice 同时提供：

1. D1 source columns/version marker，明确每个 legacy protected field、压缩
   状态、source revision 和 ciphertext scheme；
2. Workers Web Crypto 实现及专用 secret binding，配套 Python/legacy 已知
   fixture 的跨运行时 decrypt/encrypt conformance；
3. 所有 D1 reader/write path 的 transparent decrypt/encrypt 适配，包括 FTS
   与派生索引，且认证失败为 terminal error；
4. Jobs Queue consumer 的 admission、lease/retry、idempotency、per-row CAS、
   finalize atomicity 及 account-deletion fence；
5. 经过 checksum 和人工审阅的 Firestore/GCS source export、真实账号回放、
   residual scan，以及旧客户端 wire conformance。

目前尚未完成第 3、5 项，也没有 production provider/Firestore 凭据或真实
账号 live replay 证据；因此不能宣称 data-protection migration 已完成。

在这些证据齐备前，route owner/manifest 不应切换为 data-protection executor，
也不能把 D1 projection 存在本身当成历史 Firestore 加密迁移完成。

验证命令：

```bash
cd deploy/cloudflare
python -m pytest python/api-core/tests/test_migration_routes.py
npx vitest run tests/data-protection-executor.test.ts
npm run validate:manifest
```
