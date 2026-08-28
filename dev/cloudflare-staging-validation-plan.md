# Cloudflare staging 验证方案

状态：可执行草案

目标环境：`omi-cf-*-staging` 隔离资源

Web 入口：<https://omi-web-app-staging.summersmile1984.workers.dev>

Edge 入口：<https://omi-cf-edge-staging.summersmile1984.workers.dev>

## 1. 验证目标与结论口径

本方案用于证明已部署的 Cloudflare Worker slice 在真实 Web 入口、Better Auth、
Service Bindings、Python Workers、D1、R2、Queues、Durable Objects 和外部模型 API
之间能够形成可恢复的业务闭环。

验证结论分为两级，不能混用：

1. **Slice Accepted**：`manifests/routes.yaml` 中已归属 Cloudflare 的能力满足本方案
   P0/P1 门槛，可继续在隔离 staging 中测试。
2. **Product Cohort Ready**：除 Slice Accepted 外，Web 当前可见的所有操作都有已部署
   owner，P2/P3/P4 通过，且第 8 节列出的产品阻塞项已经迁移或由服务端 capability
   明确隐藏。达到这一结论前，不得将“会话列表能打开”表述为“会话产品已完整迁移”。

任何验证只使用 staging 合成账号和合成数据；禁止导入生产 token、真实录音、真实
conversation、memory 或其他 PII。

## 2. 验证角色、数据与证据

### 2.1 三类测试账号

- `empty`：全新 Better Auth 账号，验证默认状态、空列表和首次账户绑定。
- `seeded`：包含 conversation、folder、memory、action item、public app 投影，验证
  列表、搜索、详情、修改和清理。
- `media`：仅保存合成静音 WAV、R2 测试对象和异步 transcription job，验证音频、
  Queue、DLQ 和幂等边界。

账号凭据只存于权限为 `0600` 的临时 JSON 文件。发布和 smoke 使用
`CLOUDFLARE_SMOKE_TOKEN_FILE` 指向文件，日志中不得输出 token、cookie 或密码。

### 2.2 Fixture 命名与清理

每次运行生成 `run_id=cf-verify-<UTC timestamp>-<random>`，所有可写数据都以前缀
标识。最小 fixture：

- 一条无音频 conversation，含 transcript、structured summary 和 app result；
- 一个自定义 folder、一个 standalone action item、两条 memory；
- 一个小于 1 MiB 的本地生成静音 WAV；
- 一个 R2 logical key 和一个带稳定 `Idempotency-Key` 的 transcription job。

每次运行最后删除 fixture，并再次读取相应资源确认 `404` 或空集合。清理失败即本次
验证失败，不允许把残留留给下一轮。

### 2.3 证据目录

在仓库外创建 owner-only 目录，例如：

```bash
validation_dir="$(mktemp -d /tmp/omi-cf-validation.XXXXXX)"
chmod 700 "$validation_dir"
```

保存以下脱敏证据：

- `release.txt`：Git SHA、Worker version IDs、D1 migration 状态、回滚 snapshot 路径；
- `smoke.log`：`smoke:staging` 完整退出状态；
- `benchmark.log`：各 endpoint 的 p50/p95/max；
- `browser.md`：浏览器路径、可见结果、新增 console error 数；
- `workflow.md`：fixture 创建、状态迁移、幂等和清理结果；
- `observation.md`：1 小时、24 小时、72 小时的错误率、Queue/DLQ 和成本快照。

证据只能记录合成 ID；不得保存响应中的用户内容、认证头或原始音频。

## 3. P0：每次部署必须通过的自动闸门

### P0-1 发布资格与不可变基线

```bash
# Run from the repository root.
(cd deploy/cloudflare && npm run typecheck && npm test)
(cd deploy/cloudflare/python/api-core && uvx uv==0.12.3 run pytest -q)
(cd deploy/cloudflare/python/api-ai && uvx uv==0.12.3 run pytest -q)
(cd web/app && npm test)
```

通过标准：TypeScript、Python、manifest 和 Web 测试全部退出 `0`；所有 dry-run bundle
低于平台上限；`git status --short` 为空。记录待部署 SHA，不能以分支名代替 SHA。

### P0-2 资源、migration 与回滚基线

```bash
cd deploy/cloudflare
npm run validate:manifest
npx wrangler d1 migrations list omi-cf-auth-staging --remote --config workers/auth/wrangler.jsonc
npx wrangler d1 migrations list omi-cf-app-staging --remote --config python/api-core/wrangler.jsonc
```

通过标准：manifest 中没有重复或 broad `/v1/*` owner；所有已提交 migration 都是已应用
状态；本次发布前 snapshot 包含六个后端 Worker 和 Web Worker 的唯一活动版本。新 migration
必须保持 snapshot 中旧 Worker 可读，回滚不能依赖破坏性 down migration。

### P0-3 公网 readiness

```bash
curl -fsS https://omi-cf-edge-staging.summersmile1984.workers.dev/health
curl -fsS https://omi-cf-edge-staging.summersmile1984.workers.dev/ready
curl -fsS https://omi-web-app-staging.summersmile1984.workers.dev/api/worker-ready
```

通过标准：全部 HTTP `200`；Edge 版本为本次 candidate；Auth、API Core、API AI、
Realtime、Jobs 五个 Service Binding dependency 均为 `200`。Web readiness 必须经过
Web→Edge Service Binding，不能用直接请求 Edge 代替。

### P0-4 登录态公网 smoke

```bash
cd deploy/cloudflare
CLOUDFLARE_SMOKE_TOKEN_FILE=/path/to/staging-token.json npm run smoke:staging
```

通过标准：命令退出 `0`，且至少证明：

- 未认证请求按契约返回 `401/403`；
- 账户 control 为 `state=new`、允许产品流量且绑定 Cloudflare data plane；
- conversation list/search、memory、folder、settings 等 D1 路径成功；
- Web `/api/proxy` 下的 conversation、enabled apps、memories 均为 `200`；
- Worker AI 空音频边界返回预期 `400`，不触发计费推理；
- missing-row probe 的 `404` 是资源不存在，不是 Cloudflare `1042` 或 `route not migrated`。

缺少 token 时 `deploy:staging` 必须在任何远端写入之前失败。

### P0-5 真实浏览器回归

使用 in-app browser 的已有登录态执行：

1. 重新加载 `/conversations`；
2. 等待页面请求稳定至少 3 秒；
3. 确认页面无 `API error`，系统 folders 正常显示；
4. 以重新加载开始时间为界读取 console，新 `error/warn` 必须为 `0`；
5. 打开 `/memories`、`/my-apps`、`/tasks` 后返回 conversations，确认 session 不丢失；
6. 再次检查 `/api/worker-ready` 为 `200`。

仅检查 DOM 不足以通过：必须同时保存浏览器 console 时间边界和公网 smoke 结果。

## 4. P1：核心产品闭环

### P1-1 Better Auth 与请求身份

- email/password 登录后响应体不暴露 session token，浏览器仅持有 httpOnly cookie；
- 刷新、跨页面导航和 Worker cold start 后 session 仍有效；
- logout 后所有产品 API 立即返回 `401`；
- 外部伪造 `X-Omi-*` 头被 Edge 删除；
- 同一断言不能跨 audience、method、path 或过期时间重放；
- `seeded` 账号读取 `empty` 账号的每类资源均返回 `404/403`，不能返回脱敏后的他人数据。

通过标准：身份失败不能呈现为空列表成功；浏览器和 API 的用户身份一致。

### P1-2 Conversation shell 与详情

对 `empty` 账号验证正常空状态。对 `seeded` 账号依次执行：

1. list/count 显示一条 fixture，分页、日期、source、starred、discarded、folder 过滤正确；
2. 搜索 title、overview 和 transcript 均命中；标点-only 查询返回空结果而非 5xx；
3. 打开详情，summary、transcript、app result、photos/action-item 空态正确；
4. 修改 title、starred、folder、segment text、summary、event 和 action item；刷新后仍一致；
5. `GET /v1/apps/{appId}` 返回公开详情及当前账号的 `enabled` 状态；
6. 默认删除仅删除 conversation projection 并更新 folder count；`cascade=true` 必须明确
   返回未迁移错误，不能部分删除后报告失败；
7. 所有步骤完成后搜索索引不再命中已删除记录。

通过标准：每个写入都从权威 D1 回读验证；locked conversation 按契约 fail closed；无
重复 action item、folder count 漂移或跨 uid 命中。

### P1-3 Memory、App、Task 与设置

- Memory：create/list/update/visibility/review/batch delete，刷新后状态一致；
- App：public catalog、search、popular、single detail、enable/disable，private 字段不进入
  D1 projection 或响应；
- Task/folder：create、update、complete、move、delete 与 conversation 投影一致；
- 用户设置：language、transcription preference、privacy、notification、assistant、AI
  profile 的 partial update 不覆盖未提交字段；
- Goal/focus/scores/screen activity：以最小 fixture 验证 uid scope、幂等和 bounded read。

通过标准：核心路径与主要错误路径都有回读；所有创建数据在 cleanup 阶段清空。

## 5. P2：实时、媒体、异步任务和外部 API

### P2-1 R2 对象契约

使用 `/v1/cf/assets/{key}` 执行 PUT→GET→Range GET→conditional GET→DELETE：

- checksum 正确，错误 checksum 返回 `422`；
- 单 range 返回 `206` 和正确 `Content-Range`，非法/multi-range 返回 `416`；
- ETag 命中返回 `304`；
- 覆盖写不会泄漏旧 storage key；cleanup task 最终清空；
- 删除后 metadata 与 R2 object 均不可读。

### P2-2 Queue、DLQ 与 transcription

用合成静音 WAV 调用 `/v1/stt/transcribe-async`：

1. 首次请求返回稳定 `jobId`；
2. 相同 `Idempotency-Key` 和相同内容返回同一 job；
3. 相同 key、不同内容返回 `409`，且不泄漏第二个 R2 staging object；
4. poll `/v1/stt/transcribe-async/{jobId}` 和 `/v1/cf/jobs/{jobId}` 到 terminal；
5. success 路径保存规范化结果并删除临时音频；
6. 注入 provider 失败，验证 retry 次数、terminal error、DLQ 和人工 replay；
7. cron cleanup 不删除 active object，只删除已终止或过期对象。

通过标准：至少一次投递不产生重复副作用；Queue backlog 回到 `0`；非故障演练期间 DLQ
为 `0`；任何终态都没有孤立临时对象。

### P2-3 Realtime 与外部模型 API

- Web cookie 只能交换一次性、30 秒有效的 realtime ticket；重放 ticket 必须失败；
- WebSocket 首消息前发送二进制音频被关闭；合法 auth 后才建立 provider socket；
- 断线重连不让旧 socket 的 late event 覆盖新 session；多设备 session 不共享 DO；
- 对已批准的小型多语言合成集实测 streaming ASR 首字、final、断线恢复和 usage；
- prerecorded ASR、translation、TTS、embedding 使用最小计费 fixture，验证状态、响应
  schema、provider error 映射和 fallback telemetry；
- 测试日志只包含 provider/model/耗时/状态/用量，不含音频、transcript 或 prompt。

通过标准：协议、计费和删除语义一致；真实模型质量另按语言/设备/噪声集记录，不能用
“HTTP 200”替代 WER、首字延迟或主观 TTS 质量结论。

## 6. P3：安全与故障恢复

以下 fault test 优先在本地 workerd 或专用隔离 namespace 运行，不能破坏共享 staging：

- Auth/Core/AI/Jobs/Realtime 任一 binding 超时或 5xx 时，Edge `/ready` 变为 `503`；
- Python Worker cold start/abort 后下一请求可恢复，不缓存失败初始化；
- D1 transaction 冲突、Queue 重投、R2 put 成功但 metadata batch 失败均可恢复；
- request body 超限、非法 JSON、路径穿越、伪造内部头、CORS 非允许 origin 全部 fail closed；
- 非幂等写在 Edge timeout 后不自动重放；幂等写使用 receipt/fingerprint；
- account control 非 `new` 或 destination 未绑定时，产品流量被拒绝但 auth/control 可达。

共享 staging 的回滚演练需要单独批准的时间窗：

```bash
cd deploy/cloudflare
npm run rollback:staging -- .wrangler/releases/staging-before-<timestamp>.json
```

回滚后立即重复 P0-3/P0-4，并确认版本 ID 与 snapshot 一致。D1/R2/Queue 不随 Worker
版本回滚，所以还必须验证旧版本可读取新增 schema，且没有 stranded job 或 cleanup task。

## 7. P4：性能、成本与观察期

### P4-1 基线 benchmark

```bash
cd deploy/cloudflare
CLOUDFLARE_SMOKE_TOKEN_FILE=/path/to/staging-token.json \
CLOUDFLARE_BENCHMARK_ITERATIONS=20 \
CLOUDFLARE_BENCHMARK_P95_MS=4000 \
CLOUDFLARE_BENCHMARK_ENFORCE=1 \
npm run benchmark:staging
```

通过标准：六个非写 endpoint 的 warm p95 全部低于现有 4 秒门槛；同时单独记录 Python
cold start、Web `/conversations` 可交互时间、conversation search p95、Realtime 首字
延迟和 Queue terminal latency。门槛只能随着实测收紧，不能为了让发布通过而放宽。

### P4-2 观察期

- T+1h：重复 smoke/browser，检查 5xx、Auth failure、D1/R2 error、Queue/DLQ；
- T+24h：重复 benchmark，检查 Worker CPU、subrequest、AI token、R2 storage/operation、
  D1 rows/read/write 和 Queue operation；
- T+72h：确认没有新 1042、`route not migrated`、孤立 job/object 或账户状态漂移，形成
  Slice Accepted 结论。

初始停止条件：任意跨 uid 数据访问、数据损坏或认证绕过立即停止；用户路径出现
Cloudflare `1042`、连续两次 readiness degraded、5 分钟窗口 5xx 超过 1%、DLQ 非演练
增长、写入后回读不一致，立即冻结测试并按第 9 节处置。

## 8. 当前未迁移能力与验证处理

以下能力目前不属于已部署 Worker slice 的成功路径：

- conversation create/finalize、merge、reprocess、custom prompt；
- transcript speaker bulk assignment 及 speech sample 副作用；
- `/v1/sync/audio/*` URL、precache、audio merge/playback；
- cascade conversation deletion、memory extraction、downstream integration fanout；
- private app 管理、账户删除、Calendar OAuth、speaker sample、完整 vector lifecycle；
- Better Auth Google/Apple 真实 callback/link 资格检查；服务端契约已部署，但 staging
  OAuth client 凭据未配置前 capability 必须返回空 provider 列表并隐藏对应入口。

验证时必须确认这些路径不会返回假成功或完成一半。若 UI 向测试 cohort 暴露对应按钮，
则 **Product Cohort Ready 失败**；处理方式只能是迁移完整 authoritative workflow，或由
服务端 capability 明确隐藏/禁用该操作，不能用客户端吞掉 404。每次验收以
`manifests/routes.yaml` 为 owner 单一事实源，并将 Web 实际调用但 manifest 未拥有的
route 列入阻塞清单。

## 9. 判定、回滚与签字

### Slice Accepted 必须同时满足

- P0 全部通过，P1 中本次声明支持的领域全部通过；
- 浏览器本次 reload 后新增 console error/warn 为 `0`；
- 所有 Cloudflare-owned Web 请求无 `1042` 和 `route not migrated`；
- uid isolation、幂等、fixture cleanup 有回读证据；
- benchmark 通过，1h/24h/72h 观察无停止条件；
- known gaps 已按第 8 节标记，结论没有扩大到未验证能力。

### 立即回滚条件

- 身份串号、越权、cookie/token 泄漏或跨账号数据命中；
- canonical write 部分成功、重复副作用或不可恢复丢失；
- Web→Edge 再次出现 1042/404，或 readiness 连续失败；
- 5xx、DLQ、D1/R2 错误超过第 7 节门槛且无法在观察窗内恢复。

回滚使用发布前 snapshot；回滚后仍需运行 smoke 和 residual 检查。若 migration 或异步
任务使旧版本无法安全读取，停止自动回滚，隔离受影响测试账号并记录具体 job/object/
schema evidence，不得用手工删库恢复。

最终记录四个结论：验证人、candidate SHA、证据目录、`Slice Accepted` 或
`Product Cohort Ready`。没有证据目录的口头确认不计为通过。
