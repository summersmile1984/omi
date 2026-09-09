# Eddy 桌面每日使用量：实现与验证

2026-09-05，基线 `d82b9e51ba` 加本次 fork-only 变更。

## 结果与边界

Cloudflare 现在实现 `POST /v1/users/desktop-usage/daily`。它使用上游
`backend/routers/users.py` 的请求约定及
`backend/database/daily_summaries.py` 的每日设备累计语义：严格整数、计数
范围、IANA 时区、当地日期前后两天范围，一条 UID/日期/设备记录对每项
计数取最大值。Edge 保留 `users:desktop_usage_daily` 的 600 次/小时策略。

Core 使用单条 D1 upsert，避免并发读改写覆盖更大的累计值。新表加入现有
账户删除栅栏、残留检查及清理；用户导出包含这些记录。Python Worker 的
fork-owned 依赖锁加入与上游锁一致的 `pytz==2024.1` wheel/hash；原有
Pyodide 依赖版本保持不变。

这不是每日回顾生成的实现。`POST /v1/users/daily-summaries` 仍然缺少
模型生成、当地日期范围、额度、并发所有权及冷却期约定；当前重新生成
仍是固定统计文本。该缺口保留在路线迁移文档中。

## 已执行的验证

- `uvx uv==0.12.3 run pytest -q`（API Core）：427 项通过。
  新路由与用户导出的定向复验共 14 项通过，覆盖单调累计、设备/账户/日期
  隔离、非法参数、旧账户无需预置状态及删除栅栏阻止迟到写入。
- `npm test`（Cloudflare，Node 22）：111 个文件、880 项通过。
  manifest 检查覆盖路由、限流迁移清单与身份删除清单的一致性；SQLite
  行为测试执行真实迁移。初次全量检查发现新增策略清单和 trigger 声明
  约定遗漏，补齐后通过；未删除或放宽现有检查。
- `npm run typecheck`、`git diff --check`、
  `scripts/fork/check-upstream-touch.py --base upstream/main` 通过。
- `scripts/backend-python-format --write` 对 4 个相关 Python 文件检查通过；
  recording 合约使用仓库固定 Prettier。`api-core sync` 成功消费提交的
  Python 锁，无运行时锁漂移。
- `daily-product-20260905-a`：实际本地 Auth/Edge/Core/D1/Queue/R2 运行，
  recording/privacy 12 项全部通过。新 case 经真实注册会话发送并发的
  120/40/60/90 秒累计值，再通过公开导出读取：恰好一行，结果为 120。
  未认证请求返回 401，布尔计数返回 422。
- 同次运行随后通过实际 Queue 删除账户，保留生产 60 秒静默期和 30 秒
  稳定期。删除前使用量表有 1 行；两个被删账户的 169 个 App 身份字段、
  11 个 Auth 身份字段以及 R2 逻辑对象均归零。临时 App tombstone 与 Auth
  撤销栅栏按设计保留，另一账户及其附件仍可用。

公开产品路径复现命令（输出目录必须新建，Node 22）：

```sh
CLOUDFLARE_PYODIDE_CACHE_DIR=/tmp/memweft-implementation/cloudflare/runtime-cache \
node deploy/cloudflare/contracts/local-target.mjs \
  --output /Users/macstudio/.codex/eddy-production/daily-product-20260905-a \
  --brand-id daily-fixture --run-recording
```

该用例已纳入既有 `deploy/cloudflare/ci/product.sh` 入口，没有另建无 CI
调用方的测试脚本。合约输出的 `release_qualified` 仍为 `false`。ASR/模型
推理受控，应用路由、认证、存储和队列是真实代码；未验证托管模型质量
或 Vectorize 清理。

## 生产进展

本次没有远端部署或业务迁移。此前 `candidate-20260905-o` 的冻结工件
35 项本地检查通过，但不含本次每日使用量修改，需要之后重新 prepare。
20:55 的 Cloudflare API 再次观察显示 Eddy 8 个 Worker 均不存在，Auth/App D1
只有 `_cf_KV` 系统表。完整业务资格、双目标资格以及 Eddy macOS 到生产的
登录/录音/记忆闭环仍未通过。不能用本记录表述“Cloudflare 已生产可用”。

本次只读 Worker 列表和两份 D1 目录查询均返回 HTTP 200；私有原始记录为
`remote-observation-20260905-daily.json`，未输出认证凭据。
