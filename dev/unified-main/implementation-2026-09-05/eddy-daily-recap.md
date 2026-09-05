# Eddy Cloudflare 每日回顾生成与验证

2026-09-05，基线 `a7ce67f0c1` 加本次 fork-only 变更。此前 Cloudflare 缺少
按日期创建回顾的路由；重新生成只写固定统计文本。本次接入真实 Workers
AI binding 调用及 D1 生成所有权，未部署生产 Worker。

## 业务与数据边界

- API 请求先走现有额度策略，按用户已注册设备的 IANA 时区计算当地日期；
  无时区记录用 UTC，无需 FCM token。非法/未来创建日期返回 422。
- 已有日期直接复用。每个 UID/日期只有一份 D1 生成租约；并发创建返回
  409，模型调用上限 90 秒，租约 120 秒。无录音或无有效摘要内容时返回
  400 并释放租约，不消耗创建冷却期。创建成功后冷却 30 秒；重新生成在
  模型调用前占用独立的 30 秒冷却期。
- 任务、观看时长、主动提醒次数、位置以及真实记忆引用来自 D1。记忆卡
  按来源证据关联当天对话，最多三项，排除拒绝、锁定、失效、过期、受限
  敏感状态及错误账户代际。模型只能提供文字与已有来源编号。
- 模型传输失败返回 503；格式错误按上游行为降级为基本回顾并记录遥测。
  调用次数及提供方返回的 token 数写入现有 LLM usage 表；未知 token 数
  不估算。该 feature 统计没有臆造提供方费用。
- 同一 SQL 发布操作检查租约和选中对话/任务/记忆快照。模型等待期间的
  来源锁定、拒绝、删除或替换会使发布失败。重新生成保留 id、created_at、
  visibility；删除回顾撤销迟到生成者。账户删除清除回顾及控制表；导出
  包含回顾但不暴露内部生成 token。

上游行为依据：`backend/routers/users.py` 的 create/regenerate，
`backend/utils/other/notifications.py` 的空内容/并发约定，
`backend/utils/llm/external_integrations.py` 的真实任务/统计与格式降级，
以及 `backend/utils/memory/learned_today.py` 的记忆证据归属。本次替换的是
fork 内“无需模型即可生成”的旧测试；未修改上游测试或源文件。

## 已执行证据

- API Core 全量：`uvx uv==0.12.3 run pytest -q`，436 项通过。
  每日回顾定向 15 项覆盖成功、UID 隔离、复用、额度、坏日期、空白内容、
  23/25 小时夏令时、零坐标、模型失败、并发所有权、格式降级、私有分享
  字段，以及模型等待期间的来源/账户/回顾删除。
- Cloudflare：`npm test`，111 个文件、880 项通过；TypeScript typecheck
  通过。实际 FastAPI registry 与 inventory 的 619 个 HTTP/WebSocket
  slot 一致；其中 578 个有 Worker owner，41 个仍待迁移，数字不代表
  全部业务验收通过。
- `recap-product-20260905-a` 首轮及 `recap-product-20260905-b` 最终调整后
  的真实本地运行均为 recording/privacy 13 项通过。流程包含真实注册、
  Web/原生音频、转写持久化、Queue 生成记忆/任务、每日设备上报、回顾
  模型 RPC、来源引用、创建复用、重新生成和冷却期、导出及账户删除。
- 实际导出读到回顾且没有 generation_token。重复创建后 feature 调用数
  仍为 1；重新生成保留 id/首次创建时间。跨账户读取返回 404。
- 删除前 App 中有回顾和生成控制各 1 行；通过实际 Queue 及原有等待时间
  删除后，两个账户的 170 个 App 身份字段、11 个 Auth 身份字段及 R2
  逻辑对象归零；另一账户/附件保留。短期 App tombstone 与 Auth 撤销
  栅栏按设计保留。没有手工执行删除处理器或预置产品数据。
- 固定 Python/Prettier 格式、`git diff --check` 与 upstream-touch 通过。

复现（Node 22；更换为尚不存在的输出目录）：

```sh
CLOUDFLARE_PYODIDE_CACHE_DIR=/tmp/memweft-implementation/cloudflare/runtime-cache \
node deploy/cloudflare/contracts/local-target.mjs \
  --output /Users/macstudio/.codex/eddy-production/recap-product-20260905-b \
  --brand-id recap-fixture --run-recording
```

## 尚未证明

ASR/模型是受控推理 IO，应用 Worker、认证、D1/R2/Queue 运行实际代码。
本次不证明托管模型质量、托管 Vectorize 清理或 Cloudflare 生产可用性。
全部本地合约仍标记 `release_qualified: false`。

上游计划推送及 settings-test 的通知 token、当地发送时刻、聊天卡片和
推送送达仍待迁移/验收；本次仅共用该入口的回顾生成器，没有把“生成
成功”当作“通知送达”。完整 CF-4、CI-1 与生产 macOS 登录/录音/记忆闭环
仍然需要完成。旧冻结候选 o 不包含本次代码，不能用于本次源码的发布。

## 后续相邻修复：品牌导出文件名

在单独修复中，导出文件名改为经过校验的品牌 ID，例如
`eddy-export.json`。缺少品牌展示配置的旧部署仍可导出，使用中性
`user-data-export.json` 并记录共享 fallback 事件，不因展示配置阻塞
账户的隐私操作。

API Core 全量更新为 437 项通过。实际运行
`branded-export-product-20260905-a` 使用受控品牌 ID `eddy`，13 项合约通过，
公开导出响应头确认为 `attachment; filename="eddy-export.json"`。这证明
品牌 ID 被实际下载路径消费，不代表该合成配置是完整生产品牌/域名验收。
本次仍未执行任何 Cloudflare 远端写入。
