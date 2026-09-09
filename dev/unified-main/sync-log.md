# 上游同步记录

格式：`日期 | 上游 SHA | 真实冲突数 | 耗时 | 备注`

| 日期 | 上游 SHA | 上游提交数 | 真实冲突 | 备注 |
|---|---|---|---|---|
| 2026-09-03 | `6ad330a7cf` → `fd01c27267` | 1503 | 13 | 首次按单主线计划执行（S0），[PR #2](https://github.com/summersmile1984/omi/pull/2) 已合并（merge commit `0c44da31fb`）。冲突构成：6 个 fork 格式化漂移、3 个 shim 注入点（两侧合并保留）、2 个机器人/忽略文件、2 个文档与测试。**被 fork 修改的上游文件 51 → 38**；`web/admin` 与 `AGENTS.md` 归零。CI 抓到一个本地漏测的回归（加 `COPY backend/fork/` 后未重跑后端全量，pusher 源闭包三处联动声明未同步），已修并合入搬迁提交。合并后**下一次同步预演冲突数为 0**（上游已再前进 45 个提交）。 |
| 2026-09-05 | `09ff17e4e5` → `c4880cd5f6` | 2 | 0 | 合并提交 `4bf718a1cd`；两项上游提交仅将 macOS v0.12.292 changelog 归档。`upstream_sync_plan.py` 报告 `[]`。随后在同一候选上完成遗留分歧收敛：`2a18d67914` 恢复 auxiliary images，`87a67ed0a2` 将 Server OS 私有 wheels 移至 `backend/requirements-fork.txt`，`fb8eb8e438` 将 SenseVoice 选择器迁入 fork 注册表，`fe03f854b6` 恢复 upstream LLM providers，`fc74c1fdd0` 恢复 macOS AuthService 并更新 stage owner，`3fa5ce41d4` 恢复 app-client OpenAPI。候选累计检查现为 0 违规、2 个预算内白名单钩子；本地测试和镜像构建已记录，未推送或部署。 |
