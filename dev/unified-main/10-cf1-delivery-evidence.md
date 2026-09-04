# CF-1 本地交付证据（2026-09-04）

工作基线：`b9776fac12f6098ae5eed0474e42843ff507a693`（集成方案），产品源码基线
`d238a85af9d999992d9f0352db682cd9f11fc951`；短期实现分支
`codex/implement-cloudflare`。没有更改 upstream exporter、上游文件、Auth Worker
或任何已跟踪锁文件；未 push、创建 PR、合并或部署外部资源。

## 已交付边界

- fork-owned `deploy/cloudflare/scripts/route_inventory.py` 复用上游 hermetic
  bootstrap，直接枚举真实 HTTP/WebSocket 注册，包括不在 OpenAPI schema 的入口。
  重复注册同一个 method/path/protocol 只算一个外部入口；清单重复项仍拒绝。
- 当前 612 个入口：577 个 staging owner，35 个明确 blocked。旧清单的 36 个新增
  入口中，桌面 prompts 已实现；其他 35 个逐项归属和缺失契约见
  [CF-4 账本](09-cloudflare-route-migrations.md)。移除的 Crisp unread 仅从 backend
  inventory 删除；独立存在的 CF 扩展没有被错误删掉。
- `GET /v2/desktop/prompts` 由 Edge 验证身份、Core 验证请求绑定断言，从全局
  operator-owned D1 表读取。保留上游渠道、最低 build、稳定用户分桶、字段默认值、
  六项 options 上限和排序。已有用户且无配置返回空列表；鉴权失败 401，D1 或
  配置错误 503，错误 query 422。Migration 0153 不储存用户数据、不增加写接口。
- `fork-cloudflare-routes` 已注册 local/ci 两 lane。CI Node 22；OpenAPI runner
  复用已有 backend setup 的固定 Python 解释器。检查脚本不调用远端 D1 或资源创建。

## 执行记录

所有日志在 `/tmp/memweft-implementation/cloudflare/`，只有本次机器的临时证据，
重跑以仓库命令为准。下列 exit 0 仅证明列出的边界。

| 命令（仓库根，除非标出目录） | 结果 | 日志 |
| --- | --- | --- |
| `make setup` | exit 0，hooks + pinned backend | `setup.log` |
| `bash deploy/cloudflare/ci/routes.sh` | exit 0：inventory 6 tests、实际 612 注册一致、TS typecheck、107 files / 778 tests、Core 398 tests | `route-lane.log` |
| 候选树中通过 `routes.sh` 重跑 `npm run typecheck`、`npm test` | exit 0，107 files / 778 tests | `candidate-fork-lane-final.log` |
| Core 目录 `uvx uv==0.12.3 run pytest -q tests/test_desktop_prompt_routes.py` | 3 tests，包含配置错误 503 | `desktop-prompts-python-final.log` |
| CF 目录 `npx wrangler deploy --dry-run --config workers/<worker>/wrangler.jsonc`，worker 为 edge/auth/realtime/jobs/rate-limit | 5 个 exit 0 | `dry-run-<worker>.log` |
| `python/api-core`、`python/api-ai` 各目录 `uvx uv==0.12.3 run pywrangler deploy --dry-run` | 2 个 exit 0 | `dry-run-api-core.log`、`dry-run-api-ai.log` |
| CF 目录 `npx wrangler d1 migrations apply APP_DB --local --config python/api-core/wrangler.jsonc --persist-to /tmp/memweft-implementation/cloudflare/local-state` | exit 0，156 个 migration 应用成功 | `local-migrations.log` |
| Core 目录 `uvx uv==0.12.3 run pywrangler dev --local --port 18884 --persist-to /tmp/memweft-implementation/cloudflare/local-state --var INTERNAL_ASSERTION_SECRET:cf1-local-prompt-test` | exit 1：浮动 workers-py 1.17.1 要求 Wrangler >=4.127.1，仓库锁为 4.127.0 | `local-api-core.log` |
| Core 目录用上项相同参数执行 `npx wrangler dev` | 本地锁定 workerd 启动并成功响应；测试后 Ctrl-C 清理 | `local-api-core-direct.log` |
| `backend/.venv/bin/python /tmp/memweft-implementation/cloudflare/local-prompt-probe.py` | exit 0：200 返回 synthetic banner、channel/build 排除 200 空列表、缺断言/错 path 401、非法 build 422 | `local-prompt-probe.log` |

真实运行时 fixture：先用 dry-run 准备 `python_modules`，应用上表全部 migration；
将 `seed-prompts.sql` 中的唯一 synthetic global banner 写入同一 `--local`
APP_DB（`local-seed.log`）。Probe 使用当前 `internal_auth.create_request_context`
签名本地测试 uid、GET method、精确 path 和 `api-core` audience；日志不输出断言。
Local AI/Vectorize 未配置远端，prompts 不调用这些 binding。测试状态留在 `/tmp`，
不污染仓库、不使用客户数据。标准 pywrangler dev 工具闭包问题仍需后续修复。

## 证明范围与后续

Vitest 仍使用现有 Node Workers stub，Core 单测使用 SQLite+真实 ASGI 应用。
实际 workerd probe 证明本次 prompts 的 D1/HTTP 边界；它不证明完整 Auth 登录、
JWT、WebSocket、Web UI、CF-4 的业务能力或可生产部署。API AI 本次没有改业务代码，
仅重验其构建 dry-run。完整双 target 产品门禁依赖 AUTH-1/CLIENT-1/WEB-1/CI-1。

原始 `make preflight` 因 macOS 系统 Python 3.9 被子进程选中而失败；使用 fork
wrapper 的解释器选择及下述候选树 gate 重跑。最终 gate 结果随交付提交记录，
不会用未提交 diff 只检查 4 个方案文件的绿色输出来替代本次代码验收。

最终代码候选树 `a129124d2bfbccaad82df7c56b23ba09a46d2427` 的两个 gate 均 exit 0：

```bash
PATH="$PWD/backend/.venv/bin:$PATH" scripts/pr-preflight --lane local \
  --base b9776fac12 --head a129124d2bfbccaad82df7c56b23ba09a46d2427 \
  --pr-body-file /tmp/memweft-implementation/cloudflare/cf1-body.md
PATH="$PWD/backend/.venv/bin:$PATH" backend/.venv/bin/python .github/scripts/run_checks.py \
  --manifest .github/checks-manifest.fork.yaml --lane local --base b9776fac12 \
  --head a129124d2bfbccaad82df7c56b23ba09a46d2427
```

上游 gate 18 项（`candidate-preflight-final.log`）；fork gate 2 项
（`candidate-fork-lane-final.log`），包含当前 612 注册、6 个 inventory 负例/正例、
778 TS 和 398 Core tests，以及 0 upstream touches。上游 failure-class 历史
ratchet 因 shallow clone 明示 SKIP；其他检查通过。`Failure-Class: none` 的
声明及本次复用 guard 的原因在 `cf1-body.md`，使用 `scripts/failure-class validate`
验证通过（`failure-class.log`）。候选树为检查暂存内容创建的本地 Git 对象，
没有移动 main 或创建外部提交；最终分支只比该已测试树增加本节证据文本。
