# Eddy Cloudflare：生产尚未验证

本次核对时间为 **2026-09-05 21:43（北京时间）**，源码为
`codex/unified-delivery@e2d90241f3`。结论是：已有真实本地业务运行证据，
Eddy 生产应用尚未发布，不能称为 Cloudflare 生产验收通过。

## 环境与版本必须对应

| 对象 | 实际状态 | 证据边界 |
| --- | --- | --- |
| 当前 Eddy 源码 | Core 全量 437 项通过；Worker 111 文件 / 880 项通过；实际录音、回顾、导出、删除 13 项通过 | 本次读取既有日志和结果，没有把它们描述为本次重新执行。ASR/模型推理 IO 受控；不证明托管模型或生产网络 |
| 旧冻结候选 `candidate-20260905-o` | 源码 `d82b9e51ba`，摘要 `095a38c9da8d85ee9869a9a42c391ea18bf11ac8aff034873a29b7a96c7cab89`；冻结运行 35 项通过 | 不包含每日使用量、回顾和品牌导出后续提交。当前执行 `verifyCandidate` 拒绝：`source changed after qualification; prepare a new candidate` |
| Eddy Cloudflare 生产 | 8 个目标 Worker 均不存在，Auth/App 两份 D1 均只有 `_cf_KV` | 本次通过 Cloudflare API 重新读取；资源已创建不代表应用已发布 |
| Eddy macOS | 已有签名构建和本机 UI 证据 | 尚未通过生产登录 → 录音 → 转写 → 记忆/任务 → 导出闭环 |

各轮本地用例相互重叠，不能相加作为当前版本通过的独立用例总数。
所有业务运行报告的 `release_qualified` 都是 `false`。

## 本次远端读取

[脱敏原始结果](eddy-cloudflare-remote-observation.json) 的 `observed_at` 是
`2026-09-05T13:43:01.667Z`。使用已有 Wrangler 授权读取同一 Eddy 账户：

- Worker 清单：`GET /accounts/{account}/workers/scripts`，HTTP 200。
- Auth/App D1：各执行 `SELECT name FROM sqlite_master WHERE type='table' ORDER BY name`，均 HTTP 200。
- 首轮 App D1 读取曾返回 403；重新读取三项全部成功。没有据此推断授权失效或更改权限。
- 本次没有上传 Worker、写入业务数据、执行迁移或改变远端配置。

## 尚未闭合的验收

当前发布入口通过 `pendingQualifiers` 返回 `CF-4` 和 `CI-1`：
完整 Cloudflare 业务/提供方验收、同候选双目标一致性执行器尚未实现。
实际 619 个上游 HTTP/WebSocket 路由中，578 个有 Worker owner，41 个仍在
[迁移清单](../09-cloudflare-route-migrations.md) 中标记为 blocked。
路由有 owner 不等于该业务已通过生产验收。

还需完成对应业务与双目标检查，重新冻结当前源码，通过正式发布入口部署后，
取得真实 Workers AI/Vectorize 和 Eddy 原生客户端的线上闭环证据。
不能用单元测试、构建 dry-run 或旧候选通过记录代替这些步骤。

## 历史架构文档的适用范围

[`docs/cloudflare-architecture/`](../../../docs/cloudflare-architecture/README.md)
保留的是 2026-09-01 `codex/cloudflare-adaptation` 分支的
`omi-cf-*-production` / `omi-web-app-production` 记录。
其中“生产已上线”的表述不适用于本次统一主线的 `eddy-*` 环境。
整体目标架构和图集见
[统一架构文档](../architecture/three-track-architecture.md)。

本地详细证据见 [每日回顾与品牌导出](eddy-daily-recap.md)、
[冻结工件业务运行](eddy-frozen-product.md) 和
[首次生产交付记录](eddy-production-progress.md)。本次只更新验收口径，未修改产品代码。
