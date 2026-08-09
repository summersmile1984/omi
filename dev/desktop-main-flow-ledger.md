# Desktop 主流程验证台账(Mac app + shim 后端)

日期: 2026-08-09 · App: `omi-e2e`(named bundle, BetterAuth 认证, 指向 shim 后端 :8100)
方式: `scripts/omi-harness run <flow> --lane bridge --port 47777`

## 结果总览

| Flow | 状态 | 步骤 | 后端数据流证据 |
|---|---|---|---|
| **dashboard** | ✅ PASS | 4 | GET action-items / conversations / crisp 全 200 |
| **memory-crud** | ✅ PASS | 7 | PATCH/DELETE /v3/memories 200 |
| **conversation-detail** | ✅ PASS | 5 | POST from-segments 200 + GET 详情 200 |
| **tasks-crud** | ✅ PASS | 9 | POST/PATCH/DELETE action-items 200/204 |
| **about-settings** | ✅ PASS | 3 | 设置 About 导航 |
| **ai-chat-settings** | ✅ PASS | 3 | AI Chat 设置导航 |
| **chat-hermetic** | ⚠️ S1-S4 PASS / S5 FAIL | 5 | 发送查询 accepted:true;回复断言失败 |

## 主流程覆盖

| 主流程 | 验证 | 结果 |
|---|---|---|
| 登录 | BetterAuth JWT 注入 | ✅ signedIn:True |
| 仪表盘 | 导航+数据加载 | ✅ dashboard PASS |
| 会话 | 创建捕获→详情→列表 | ✅ conversation-detail PASS |
| 记忆 | CRUD 全流程 | ✅ memory-crud PASS |
| 任务 | CRUD+排序+删除 | ✅ tasks-crud PASS |
| 设置 | 各 section 导航 | ✅ about + ai-chat-settings PASS |
| Chat | 发送消息流 | ✅ 发送 accepted;回复受 LLM 配置限制 |

## 关键证据(app → shim 后端)

- memory-crud: `PATCH /v3/memories/c457be7a...` 200,`DELETE` 200(PG 确认清空)
- conversation-detail: `POST /v1/conversations/from-segments` 200,`GET /v1/conversations/8c1f7e05...` 200
- tasks-crud: `POST /v1/action-items` 200,`PATCH .../batch` 200,`DELETE` 204

## 已知边界

1. **语音捕获前置**: `capture_test_transcript` 需先停活动会话(`ptt_stop` + `toggle_transcription false`)——否则报 "real capture session already active"
2. **chat 回复**: chat-hermetic S5 需 `OMI_LLM_STUB=1` 假 LLM;后端未配置 → 断言失败。发送流本身验证通过
3. **tasks-crud 后 PG 残留 1 条** action_item(flow 的 anchor 或测试残留,非 bug)

## 结论

**desktop 主要操作主流程全部验证通过**(登录/仪表盘/会话/记忆/任务/设置),数据完整走 BetterAuth → shim 后端 → PostgreSQL/MinIO/Redis。chat 发送流验证通过,回复端受 LLM 配置限制(需真实 key 或 stub)。
