# Eddy 首次发布：数据库资格入口

2026-09-05，在 `fc948dd517` 之后实现固定的 `qualify-prior-schema.mjs` 入口。
它完成的是此次实际首次发布的数据库前置资格，不能替代 CF-4 产品链路或
CI-1 双目标验收，也不认证既有 Worker 的升级/回滚。

## 已执行的真实证据

- 候选：`/Users/macstudio/.codex/eddy-production/candidate-20260905-h`。
  digest `91afbafdcd97ffafe4c1e4d9d7d9f2043e9848e2621f879adb9c00060094db79`。
  来源为本次提交前的精确工作区文件；后续提交和改动必须重新 prepare。
- 完整 prepare exit 0：111 files /870 Cloudflare tests、Core 415、AI 131、
  两目标 Web 检查/构建、SQL fixtures、8 Worker 源码及冻结上传 dry-run 全通过。
- 固定 schema 入口作为实际 Node 进程执行，输入含候选目录、候选对象和实时观测。
  它独立重新查询了 8 个 Eddy Worker，均为 absent；确认 Auth/App D1 的实际
  UUID 与候选相同，业务结构和迁移记录均为空。
- 子进程读取候选的 `sql/` 字节，执行全部迁移、旧用户/会话/任务保持、重入及
  外键检查。未从可变源码目录替换 SQL。业务数据只在本地隔离 SQLite 夹具中创建。
- 结果 exit 0，5 个 `first-release.*` cases 全通过，observation digest
  `82d295827f1cc2cc6a990b596e35b6b30e13fff3a9f6cf188c88a76af25d894a`。
  私有原始日志、观测与结果分别是 `schema-qualification-20260905-a.log`、
  `schema-qualification-20260905-a-observations.json` 和
  `schema-qualification-20260905-a-proof.json`，均位于上述私有证据目录。
- canonical `release.mjs check` 验证相同候选通过，缺失执行器为 `CF-4` 和 `CI-1`。
  `release_ready` 仍为 false；该 standalone 结果不能作为后续 apply 的通行文件，
  正式事务必须重新执行固定入口。

## 验收边界

发布器现在将自己验证过的 `candidate_directory` 传给固定入口。入口重新打开
候选并比较源码、工件和 stdin 对象；结束前再验证一次，拒绝替换对象或中途修改。
SQL 子进程保持在上层发布进程组内，且自身有一分钟上限。

`candidate` 阶段确认首次缺席和空业务结构。`deployed` 阶段还要求实际 Worker
携带该 candidate/artifact 标识、D1 完整迁移记录、与本地执行结果完全一致的
schema catalog，以及空的 `PRAGMA foreign_key_check` 结果。后半段目前只有
受控 API 边界测试，必须在真正迁移/发布后再对 Cloudflare 运行，不能据此声称
线上部署完成。

存在旧 Worker、首次业务库非空、数据库身份不符、版本变化、错误权限、SQL
变更、结构/迁移记录漂移和外键错误均拒绝。已有版本的升级以及 `restore` 阶段
需要真正运行保留版本的兼容夹具；不会把本地 SQL 成功当作旧 Worker 兼容。

相关 58 项测试及完整 870 项 Cloudflare 测试通过；用例接入既有 Vitest
local/CI lane，未新增脱离日常入口的检查脚本。D1 的外键检查能力依据
[Cloudflare SQL statements](https://developers.cloudflare.com/d1/sql-api/sql-statements/)。
