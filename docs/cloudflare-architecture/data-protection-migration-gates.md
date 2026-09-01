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
`cf_data_protection_migration_runs` receipt/lease 草案，尚未提供可证明上述
契约的 source representation：

- `cf_memories.content` 是 plaintext，且没有 legacy `evidence` 字段或加密
  状态列；
- `cf_conversations` 没有 `data_protection_level`，其
  `structured_json`、`transcript_segments_json` 和 FTS searchable text
  没有加密/清除契约；
- `cf_chat_messages` 没有 `data_protection_level`，`message_json` 内嵌
  `text` 没有可区分的 encrypted envelope；
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

## 开放 executor 前置条件

需要一个独立可审阅的 migration slice 同时提供：

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

在这些证据齐备前，route owner/manifest 不应切换为 data-protection executor，
也不能把 D1 projection 存在本身当成历史 Firestore 加密迁移完成。

验证命令：

```bash
cd deploy/cloudflare
python -m pytest python/api-core/tests/test_migration_routes.py
```
