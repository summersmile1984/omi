# Web 全量功能验证台账 (ego-lite)

日期: 2026-08-09 · 后端: 127.0.0.1:8100 (3.11 venv + FIRESTORE_PG_DSN shim + 三 emulator)
方式: 从 `/openapi.json` 全量盘点 559 操作,ego-lite(ego-browser)逐端点探测
探测面: 非破坏性 479 端点(排除 delete-account/knowledge-graph/import/admin/dev 等破坏性 75 个)
认证: Firebase Auth emulator 签发的新用户 token(`webverify2@omi.local`)

## 全量扫描结果 (479 个安全端点,新鲜用户)

| 状态码 | 数量 | 含义 |
|---|---|---|
| 200 | 156 | 正常响应 |
| 400 | 8 | 请求参数/业务校验(占位数据) |
| 401 | 25 | 需要特定认证面(23 个 MCP 端点走 MCP auth) |
| 403 | 9 | 功能/作用域门禁(phone/memory 全局读门/proxy 网关/agent) |
| 404 | 90 | 占位 ID 文档不存在(probe-* 路径参数) |
| 422 | 180 | 写端点最小 body 未过校验(探测用占位 payload) |
| 500 | 10 | 全部为环境/数据态,非 shim bug(见下) |
| 503 | 1 | `/ready` 就绪探测(启动期) |

## 500 明细 — 全部环境/数据态,零 shim bug

| 端点 | 原因 |
|---|---|
| GET /v1/task-integrations/{key}/oauth-url | `BASE_API_URL not configured`(OAuth 集成未配) |
| GET /v1/integrations/{key}/oauth-url | 同上 |
| GET/DELETE /v1/messages, GET/DELETE /v2/messages, POST /v1/initial-message, POST /v2/initial-message | `OpenAIError: Missing credentials`(offline 无 OPENAI_API_KEY) |
| POST /v1/user/persona | 同上(LLM 生成 persona) |
| DELETE /v1/apps/{id}/subscription | 数据态:占位 app 无活跃订阅(未处理错误路径) |
| POST /v2/agent/provision | OpenAI credentials(LLM) |

## 本轮发现并修复的 shim 缺口 (6 个)

1. **`Query.offset()` 缺失** — conversations/memories 列表 500(16+ 处 database/* 使用)
2. **`Query.count()` / `CollectionReference.count()` 缺失** — count 端点 500;返回 `[[result]]` 嵌套(匹配 `result[0][0].value`)
3. **`BaseCompositeFilter`(AND)未支持** — apps 列表 500
4. **`array_contains`/`array_contains_any` 下划线变体缺失** — personas 500
5. **`DocumentReference.collections()` 缺失** — 删除会话/账户擦除 500
6. **`CollectionReference.select()` 缺失** — action_items ids 500;投影已接入查询链路
7. **`DocumentReference.get(['field'])` 投影参数** — users settings 500(`get(['language'])` 等)
8. **`DocumentSnapshot.to_dict()` 空投影返回 None** — 应返回 `{}`(匹配真 SDK)
9. **`stream(retry=)` kwargs** — trends 500
10. **`Client.batch()` 缺失** — memories 删除 500
11. **`__name__`(文档 ID)字段查询** — subscription/usage-quota 500(翻译到 doc_id 列,DocumentReference 值取 .id)

## 各域结论

- **域1 认证+用户**: profile/onboarding/perm/byok/people/language/subscription/usage-quota/ai-profile/transcription-preferences 全通
- **域2 内容**: conversations/memories/action-items/goals/folders 核心端点全通(offset/count 修复后)
- **域3 应用市场**: apps/app-categories/apps-enabled 通(BaseCompositeFilter 修复后);personas 数据态 404
- **域4 会话/消息**: 列表端点通;写端点/initial-message 需 LLM key(env)
- **域5 任务/集成**: 列表通;oauth-url 需集成配置(env)
- **域6 管理/杂项**: admin/developer 需 admin key(403/401,预期);MCP 走 MCP auth(401,预期)

## 回归

- 影子对拍: **19/19** 场景与真 SDK 一致
- firestore_pg 测试套件: 10/10 通过
- 后端干净启动 health 200

## 破坏性端点 (75 个,未执行,避免污染数据)
DELETE /v1/users/delete-account、/v1/knowledge-graph、/v1/import/*、/v1/admin/*、/v1/dev/*、payment/stripe 等。
注: 首轮曾误触发 `DELETE /v1/users/delete-account` → 该 uid 进入 `account_deletions wipe_status=failed`,导致该 uid 全部端点 403 `account_deletion_in_progress`(这是**正确的删除围栏行为**,非 bug;已清除标记恢复)。
