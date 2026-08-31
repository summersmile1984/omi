# App-owner data attestation operator

`deploy/cloudflare/scripts/app-owner-attestation.mjs` 是一个纯本地的审阅包生成器，
用于把 Persona/App history planner 和 chat-history planner 的完整性结果汇总成
`0127_app_owner_data_projection_attestation.sql` 所需的 attestation body。它只读取本地
JSON，不连接 Firestore、D1、R2 或 provider，不调用 admin endpoint，也不执行 memory
re-encryption。

## 使用

两个输入可以是 planner 输出，也可以是对应的原始 manifest：

```bash
npm run app-owner:attestation -- \
  --persona-plan /path/to/persona-plan-or-manifest.json \
  --chat-plan /path/to/chat-plan-or-manifest.json \
  --source-uid fb-anon-<64 lowercase hex> \
  --source-proof-hash <64 lowercase hex> \
  --source-projection-revision <64 lowercase hex> \
  --target-uid <better-auth-uid> \
  --target-account-generation 7 \
  --memory-projection-count 0 \
  --memory-reencryption-status not_required \
  --format bundle
```

原始 manifest 会先经过对应 planner；planner 的 public-only Persona row（没有
`private_envelope`）也是合法输入。CLI 对文件使用 fatal UTF-8 decoder，遇到 malformed
bytes 或 JSON 解析失败会以非零退出，不会把替换字符混入 revision。

`--persona-input`/`--chat-input` 是 `--persona-plan`/`--chat-plan` 的等价别名。
`target-uid` 和 generation 在两个 planner 都有唯一一致的 staged row 时可以省略，
但 source uid、source proof hash、source projection revision 和 memory 证据必须明确
提供。source uid 必须是 `fb-anon-<sha256>`；CLI 不接受或输出 raw Firebase UID/token。

输出格式：

- `bundle`（默认）输出带 `attestation`、planner digest/count evidence、safety flags
  和 `review_sql` 的 JSON 审阅包；
- `json` 只输出 JSON 审阅包；
- `sql` 输出只读的 `SELECT` 审阅 SQL。SQL 明确标记 `offline_only_no_write`，不包含
  `INSERT`、`UPDATE` 或 `DELETE`，不能绕过 admin writer。

Persona planner 的 staged row 数作为 `app_projection_count`，并受 Jobs executor 的
500 行上限约束；chat session/message 数只作为完整性证据，不会被误计为 memory。
两个 planner 只要有 blocked row、重复冲突、source/target/generation 不一致或自身
manifest checksum 不匹配，CLI 就以非零退出并不生成 attestation。

Memory 约束是 fail-closed 的：

- `memory_projection_count=0` 必须使用 `memory_reencryption_status=not_required`，且
  revision 必须为空；
- count 大于零必须使用 `completed` 并提供真实的 64 位 SHA-256
  `memory_reencryption_revision`；
- CLI 不会把 chat history 当作 memory，也不会猜测、生成或执行 re-encryption。

`data_projection_revision` 是由 source proof/revision、目标 generation、两个 planner
manifest/row digests、app/memory counts 及 memory evidence 计算出的 SHA-256。该 revision
和 JSON 中的 planner digest 让人工 reviewer 能确认审阅的输入没有被静默替换。审阅通过
后，operator 仍需在独立、受 gate 保护的 workflow 中提交 `attestation`；本 CLI 本身
不会发起该请求。真实 Firestore export/import、D1 projection 和 memory re-encryption
完成前，exact `/v1/apps/migrate-owner` 虽已由 gated Edge→Jobs adapter 承载，但必须保持
exact gate 关闭；它不能绕过真实 Firestore export/import、D1 projection、memory
re-encryption 与 provider continuity 门槛。
