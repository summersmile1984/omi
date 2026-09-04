# CF-5：当前源码发布候选与事务入口（2026-09-04）

本包基于整合提交 `701daa225d`，不修改上游、锁文件或原上游 CI。
`deploy:staging` / `deploy:production` 已迁到同一 `release.mjs`，消费当前
Moonshine/Bun 双目标构建器、CF3 资源计划及固定 Python 入口。旧 Next/vinext
publisher、仅供它们使用的快照/迁移/生成文件 helper 和旧参数形状一并退休。
完整运行说明在 [release.md](../../deploy/cloudflare/release.md)。

本包不是 Cloudflare 全产品上线证明。CF-4 的实际路由/provider 产品契约、
CI-1 的双目标一致性、旧 Worker 对新 schema 的兼容证明仍缺失；三个固定
runner 缺失时 `apply` 和 `restore` 在任何 Cloudflare 请求之前拒绝。
没有创建远端资源、上传 secret、修改域名、远端迁移、发布或回滚 Worker。

## 实际本地证据

日志根目录：`/tmp/memweft-implementation/cloudflare/release/`。
合成品牌 `cf-alpha` 的同一 beta stage 同时声明 Server OS 与 Cloudflare URLs；
账号 ID、D1 UUID、域名均为工程 fixture，secret 输入只包含环境变量**名称**。
它不是完整白牌资产或真实资源资格证明。

| 命令/表面 | 实际结果 | 日志 |
| --- | --- | --- |
| `make setup` | exit 0，独立 worktree/hook/backend 环境就绪 | `setup.log` |
| 原固定 Python Core `deploy --dry-run --outdir` | exit 0，完整 Python entry 与依赖输出 | `python-shape.log` |
| 直接把原 dry-run 目录当 Python module root | exit 1，Wrangler 明确拒绝 module root 内的 `python_modules` | `python-frozen.log` |
| 按固定 Wrangler 4.127.0 规则把 vendored 依赖归 project root 后 `--no-bundle --dry-run` | exit 0；没有忽略检查或升级工具 | `python-portable.log` |
| 初版完整 prepare 用 backend pytest 环境执行 Worker tests | exit 1，6 个 collection error：缺 `workers` module；改为已有 CF CI 的 `uvx uv==0.12.3 run pytest -q` | `candidate-beta/logs/core.log` |
| `deploy:staging --manifest ... --inventory ... --output .../candidate-beta-2` | exit 0；8项本地资格、两 Web 各26routes、8次源码编译+8次冻结 dry-run | `prepare-beta-2.log` / `candidate-beta-2/logs/*` |
| 上一候选 CF suite | 106 files /806 tests 全过；这是后续发布恢复测试扩充前计数 | `candidate-beta-2/logs/workers.log` |
| Core / AI | 398 /118 全过；Python测试使用CPU/dev SDK环境，非真实workerd产品端到端测试 | `candidate-beta-2/logs/{core,ai}.log` |
| Web上游 / CLIENT / builder | 原Web类型、417 Vitest+5 Moonshine；13 session+6 UI/policy；builder3 tests/20assertions，全部通过 | `candidate-beta-2/logs/web-{upstream,client,builder}.log` |
| SQL fixture | Auth10/App156，旧用户/会话/任务保留，再入0；旧Worker兼容标记仍false | `candidate-beta-2/logs/sql-fixtures.log` |
| 实际 CLI `check` | exit 0，candidate digest `2afac8165c88d63c4159c9b01e1bb1a1dc4e80259b7dfe7b6003d330dc146365` | `check-beta-2.log` |
| 实际 CLI `apply` 给同候选精确digest但无资格runner | exit 1，明确 `CF-4, CI-1, prior-schema` pending；未创建journal/未调用API | `apply-pending.log` |
| 独立 `release dry-run` | exit 0，8 Worker重复冻结dry-run；这一候选合计24次Worker dry-run | `recheck-beta-2.log` |
| 新发布边界测试 | 3 files /35 tests全过；执行实际事务、HTTP/子进程与输出所有权函数 | `contracts-final.log` |

上面候选包含实际 base SHA、dirty source逐文件hash、双profile、资源计划、
166份SQL、锁文件、运行时版本、8 Worker配置/代码/依赖/静态资源和Bun工件hash。
额外检查8/8 Worker冻结模块与第二次 Wrangler输出相同；Core430、AI363为
包含README的输出文件数，实际上传module计数在最终候选单独记录。
README与未上传的esbuild根source map不是上传module；其余模块必须逐字节一致。
该检查已固化到 prepare/dry-run 的生产函数，并有依赖内容变化的行为负例。

候选之后增加的恢复错误路径、journal完整性和上传模块校验有独立测试；
任何源码/提交变化都会使旧candidate的`check`失败，不能把beta-2的资格嫁接
到集成提交。最终提交/集成树必须重新 prepare；最终门禁与再构建日志由本次
交付的 `formal-*.log` 和 `committed-prepare.log` 记录，失败不会产生可发布metadata。

## 恢复/权限边界

新测试覆盖：缺资格/source变化/磁盘失败时零远端写；旧D1 ledger是精确前缀；
事务 intent先fsync；未知创建结果不可收养同名资源；实际create ID写回inventory
后必须重建；旧/首发/分流版本明确区分；发布进程失败而版本已激活只可证明归属，
不能证明domain/trigger完整；反向依赖恢复只作用于本事务当前持有的版本；
部分SQL未知、并发版本、篡改journal、恢复后健康失败不能写成恢复完成。
Worker恢复不回滚SQL，不删除数据资源、首发Worker或猜测queue/domain/DO归属。

未来真实 apply使用固定Wrangler绝对路径，冻结config和`--no-bundle --strict`。
临时secret文件权限0600、单次调用后移除；过程输出/API错误正文不进入journal。
候选资格和部署后的产品验收分别执行固定runner，绑定当前candidate与观测digest。
operator提供`approved=true`不能替代执行证据。实际remote API response、domain/
queue/metadata传播、真实创建后的ID和生产provider仍待授权后的验收；本包只用
可控HTTP/进程seam，不伪造远端发布版本或上线批准。
