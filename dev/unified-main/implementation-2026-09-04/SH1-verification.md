# SH-1 启动边界实施与验证

基线 `b9776fac12`（`origin/main d238a85` + 审计），依赖 WL-1 `7e57296849`。
本包全部使用 fork-owned 路径，没有修改 upstream 文件或扩大 allowlist。

已实现：独立 self-host 镜像层在构建时生成 target/stage profile，显式 API/worker
bootstrap，版本化 PG v3 与 `python -m fork.migrate migrate|check`，四队列配置及
派发凭据统一 owner，消费者退出使 supervisor/container 失败。Python 普通
`sitecustomize` 异常不再承担启动安全职责。启动工具检查真实命令闭包并先构建镜像。

## 本机验证

日志均在 `/tmp/memweft-implementation/server/`。测试只使用本次创建的隔离资源、
随机凭据和 synthetic 数据；没有操作既有服务或生产部署。

| 验证 | 结果 / 日志 |
| --- | --- |
| `make setup` | exit 0；`make-setup.log` |
| `PYTHON=<worktree>/backend/.venv/bin/python BACKEND_UNIT_TEST_FILE_LIST=/tmp/memweft-implementation/server/focused-tests.txt bash backend/test.sh` | 7 文件、52 tests pass；`startup-tests-focused.log` |
| `PYTHON=<venv> bash deploy/self-host/operations.sh self-check` | exit 0；`operations-selfcheck.log` |
| `backend/.venv/bin/python deploy/self-host/check-config.py --env-file <synthetic fixture.env>` | exit 0；`fixture-config.log` |
| upstream Dockerfile + `deploy/self-host/Dockerfile`，linux/amd64 构建 | exit 0；`base-build-final.log`、`fork-build.log` |
| 镜像内容 | `self_hosted.local` profile、sherpa-onnx 1.13.4、无本地 `.venv`；`image-content.log` |
| fresh PG migrate / repeat / check | exit 0，schema v3；`pg-fresh.log`、`pg-repeat.log`、`pg-check.log` |
| 独立 v2 数据库升级 | v2 admission 拒绝；v3 成功、保留 sentinel 数据；重复迁移成功；`pg-upgrade.log` |
| 从真实 Compose 导出的 Python 服务 fixture | migration exit 0、API `/ready` 200、4 consumers 启动；`compose-migrate.log`、`compose-runtime.log` |
| 实际四条 HTTP 内部队列路由 | own key 接受合成无效负载并返回 200-dropped；其他 3 key 和 invalid key 全部 403；`live-queue-probe.log` |
| 实际 Redis → 4 consumer → ASGI | 合成无副作用任务全部排空；同上 |
| image 启动故障 | API/worker 的坏 profile、`python -S` 缺依赖均 exit 1；终止一个 consumer → supervisor/container exit 1；`fault-probe.log` |

声明 `Failure-Class: FC-runtime-image-boundary`。新 fork manifest startup gate
在 local/CI 均发现行为回归测试和命令闭包静态检查，针对已合并 fork PR #7 的真实
缺失命令/未生成 profile 故障。与上游的 image registry 分开是 zero-touch 约束，
共用运行时契约由 typed bootstrap / Queue API 承担。

## 仍未证明 / 后续包

- 以上 Compose fixture 保留生产文件 Python 服务的 command、image 和 environment，
  使用单独 network、PG、Redis，移除了其他 provider 依赖及模型挂载。因此 `/ready`
  与队列 no-op 不能证明 LLM、STT、向量、push、对象存储或完整业务链可用（SH-2/SH-3）。
- 新 profile 全名现已被直接 egress guard 识别，但该函数不能约束所有 socket；
  启动日志仍能看到上游 premium provider 路由，需要 SH-3 审计全部 provider owner。
- 完整 fork 目录测试发现既有 `test_export_cloudflare_x_posts.py` collection error，
  原因是被测 `backend/scripts/export_cloudflare_x_posts.py` 缺失（CF 后续业务包）。
- 既有 PG live suite 为 20 pass / 2 fail，分别缺 `users.count_user_owned_rows`
  和 `account_deletion_policy.account_deletion_receipt_id`（SH-2）；没有删除或隐藏它们。
- source-write freeze / reconcile / Firestore import 的完整 CLI 闭包仍属于 SH-4；
  `zero-vendor-acceptance.sh --self-check` 不会因 startup check 通过而冒充 full cutover 成功。
- 更早的 custom-schema 升级实验暴露 `create_composite_indexes` 跨 schema 表名判断问题；
  正式 v2→v3 验证已改为独立数据库。多 schema 支持不在本包承诺范围。
- 没有执行生产 `operations start`、发布或 cutover。
