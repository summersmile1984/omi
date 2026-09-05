# Cloudflare 日历录音缺口查询验证

`GET /v1/calendar/capture-gaps` 已由 Edge 路由到 Jobs。它复用现有 Google
Calendar 加密授权及令牌刷新，对接 `cf_conversations` 的当前用户数据；新迁移
`0159_calendar_capture_gap_range.sql` 增加开始时间索引，没有创建第二份录音数据。

业务规则来自上游 `backend/routers/google_calendar.py` 和
`utils/conversations/calendar_linking.py::select_capture_gaps`：查询最多 31 天，
最多读取 250 个事件和 500 条按开始时间倒序的录音，录音窗口向前补 24 小时。
会议须已确认、未取消且用户未拒绝，全天及超过 8 小时的事件排除；被丢弃的录音
不覆盖会议，至少重叠 10 秒才算已录制。响应只报告缺口，录音记录保持不变。

## 已执行

- Cloudflare `npm run typecheck`：通过。
- Cloudflare `npm test`：111 文件、884 测试通过；日历文件 23 测试通过。
  新行为测试执行真实 Hono 路由、加密授权写入和全量迁移后的 SQLite：覆盖
  十秒边界、跨用户隔离、跨午夜、排除条件、查询上限、索引选择、401 刷新、
  提供商错误/畸形响应、D1 故障以及断开连接。Google HTTP 响应受控，未访问真人日历。
- 正常源码构建的本地 Cloudflare workerd，`local-target.mjs --run-core`：11/11。
- 现有 Server OS 正常 Docker 镜像 `memweft-contract-a44e046d403a-api`，同一
  `contracts/deployment/core.py`：11/11。新增 HTTP 用例验证未授权、缺失/非法
  日期、倒置/过长窗口和带时区的未连接查询。Server 未改业务实现。
- `npm run validate:backend-routes`：619 槽位与实际 FastAPI 注册一致；现为
  581 Worker-owned、38 blocked。普通清单检查随 `npm test` 通过。

初次日历测试的一项失败来自预期文案：共享提供商 JSON 解码器对非 JSON 错误
返回自身的已脱敏错误。测试现分别验证非 JSON、JSON 500 和畸形事件数组的实际
错误边界，没有改为吞错或放行空结果。修改均在 fork 文件内，默认 LLM 提示词未改。

私有原始证据：`/Users/macstudio/.codex/eddy-production/` 下
`calendar-cf-typecheck-20260906.log`、`calendar-cf-vitest-20260906.log`、
`calendar-routes-20260906.log`、`calendar-cf-20260906-a/trace/core-results.json`
和 `calendar-server-20260906-a/core.log`。

## 验证边界

未连接真人 Google Calendar 账户，未执行远端 Eddy Worker 端到端成功查询；
本轮没有应用生产 D1 迁移或部署 Worker。这不是 CF-4 / CI-1 发布资格，也不代表
macOS 与生产环境已联通。本机 AI 仍沿用 MiMo LLM/ASR/TTS 和本地 BGE-M3。
