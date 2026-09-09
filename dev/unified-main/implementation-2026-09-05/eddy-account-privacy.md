# Eddy Cloudflare 新账户归属与隐私验收

2026-09-05。基线 `ce87ffd4d5` 加本次 fork-only 修复。此记录证明本地真实
Cloudflare 运行时的业务行为，不代表应用已在生产发布。

## 实际发现与修复

运行 `privacy-product-20260905-a` 时，公开注册、原生/Web 录音、转写持久化、
重连、实际队列生成记忆/任务、注销与重新登录、完整导出共 7 项通过；随后
`DELETE /v1/users/delete-account` 实际返回 503。原因是新 allocation 的配置
关闭了账户归属初始化，普通业务请求也不读取归属，而 Jobs 删除入口要求
存在已完成且绑定目标数据库的归属记录。

修复复用 Core 现有的原子初始化，保持单一归属所有者。新 allocation 在
Core/Edge/Realtime 启用 `ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED`，迁移 UI 能力
仍独立配置。产品请求读取归属和删除栅栏；隐私请求读取/初始化归属后交由
Jobs 判定，因此注册后直接删除也可用。原有归属不会被覆盖，Firebase 主体
不会被自动初始化；依赖不可用返回 503，迁移中普通业务仍被拒绝，隐私操作
保持可达。冻结测试保留候选的 bootstrap 配置，不能为了通过测试改变它。

## 验证证据

- 相关 4 个测试文件 176 项通过；全量 Cloudflare Vitest 111 文件、880 项通过。
  TypeScript typecheck、diff whitespace 与 upstream-touch 检查通过。
- `privacy-product-20260905-b` 实际 public HTTP/WS/Queue 路径通过 10 项。
  运行结束后只读核查两个被删账户：App/Auth/R2 均无逻辑残留，另一账户和
  附件保留。该次存储核查为事后独立执行。
- `privacy-product-20260905-c` 将只读存储核查纳入既有 recording 产品测试，
  执行 core 8、recording/privacy 11、chat 11、share 2，共 32 项，全部通过。
  每次使用新的本地 D1/R2/DO/Queue 与应用 Worker，未预置业务数据或手工
  调用删除处理器，未缩短生产的 60 秒静默期和 30 秒稳定期。
- 删除前实际有录音、记忆、任务、认证主体和 1 个 R2 附件。删除后检查
  168 个 App 身份字段和 11 个 Auth 身份字段，两个账户的计数均为零，
  R2 对象均为零，删除任务已结束，各保留 1 个临时 App tombstone。
  Auth 持久撤销栅栏按设计保留。原凭据无法登录，另一账户仍可读回附件。
- R2 错误校验和返回 422；对象读回字节与上传一致；跨账户读取返回 404；
  导出包含真实 Auth 姓名/email、完成的两段录音、衍生记忆和任务。

复现命令（Node 22，需已经存在的已校验 Pyodide cache）：

```sh
CLOUDFLARE_PYODIDE_CACHE_DIR=/tmp/memweft-implementation/cloudflare/runtime-cache \
node deploy/cloudflare/contracts/local-target.mjs \
  --output /Users/macstudio/.codex/eddy-production/privacy-product-20260905-c \
  --brand-id privacy-fixture --run-core --run-recording --run-chat --run-share
```

复现时必须换一个尚不存在的输出目录。`trace/recording-http.jsonl` 记录路径和
状态码；`trace/privacy-storage-results.json` 记录删除前后计数，不含凭据或用户
内容。`fixture.json` 记录应用工件、配置、迁移和工具版本。

## 验证边界与剩余工作

ASR/模型是受控推理 IO，应用、认证、数据库和队列执行真实代码。本次证明
R2 逻辑对象删除，不证明磁盘回收或托管 Vectorize 删除；模型质量与托管服务
仍须线上验证。导出的文件名仍为 `omi-export.json`，其白牌展示后续需要修复。

正式发布仍需冻结候选复验、CF-4 完整业务执行器、CI-1 双目标执行器，以及
生产部署后的 Eddy macOS 登录/录音/记忆闭环。本次没有发布 Worker、上传
应用密钥或执行远端业务迁移；所有本地报告仍为 `release_qualified: false`。

20:16（Asia/Shanghai）通过已授权 Cloudflare API 再次只读查询：Eddy 的
8 个生产 Worker 均不存在，Auth/App 两份 D1 都只有 `_cf_KV` 系统表。
证据保存在私有 `remote-observation-20260905-privacy.json`，不是根据本地
配置推断的部署状态。首次并发读取遇到 403；随后用现有 Wrangler 授权
逐项重读，Worker 列表和两份数据库目录均返回 200。
