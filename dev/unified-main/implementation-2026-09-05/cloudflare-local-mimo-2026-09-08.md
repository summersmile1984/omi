# Cloudflare 本地真实 MiMo 联调 — 2026-09-08

Wrangler 本地业务可以继续调用 `env.AI.run()`，由本地专用 Provider RPC
转接 MiMo China Token Plan 的 `mimo-v2.5`。生产 Worker 入口、Workers AI
绑定和默认提示词没有修改。

## 已完成的实际验证

完整命令从仓库根目录执行，使用 Node 22、已安装的 API Core Python 和私有凭据文件：

```sh
npm --prefix deploy/cloudflare run test:product -- \
  --llm-dev-vars /Users/macstudio/.codex/eddy-production/cloudflare-mimo.dev.vars
```

该命令退出 0，真实应用 Worker 启动、Auth/App 迁移、公共接口和聊天回归均完成，
随后自动关闭自己创建的运行进程。

| 验证                                           | 结果      |
| ---------------------------------------------- | --------- |
| 公共 HTTP：身份、记忆、任务、账号隔离等        | 16 项通过 |
| 上游 Web 客户端 → SSE → 实际 MiMo 回复         | 通过      |
| 回复和所选会话历史在 D1 中保存、重新读取       | 通过      |
| 另一账号读取该会话被拒绝                       | 通过      |
| JSON Schema → MiMo 工具输出 → 业务目标建议解析 | 通过      |
| 清空后旧回复不能从历史读回                     | 通过      |

两次业务模型调用返回的实际用量分别为输入 72 / 输出 52、输入 414 / 输出 53。
没有用固定回答替代失败，也没有改写消息或追加默认系统提示词。

- 汇总及原始回归副本：`/Users/macstudio/.codex/eddy-production/cf-live-llm-evidence-20260908/`
- 实际运行产物及私有日志：`/var/folders/v5/vwp83hn54v75tx3l7kqjdp7w0000gn/T/memweft-cloudflare-product.Phbk2d/target/`
- 完整组件 `npm test`：132 个文件、1089 项通过；日志位于
  `/Users/macstudio/.codex/eddy-production/cf-dev-llm-vitest-20260908.log`。
- 最终适配器及 workerd 专项：21 项通过。
- `git diff --check` 通过；改动源码中没有 MiMo 凭据。

## 这次实测修正的问题

最初 Node 直连成功，但 Worker 返回 502。实际锁定的 workerd 拒绝
`redirect: "error"`；改成 `manual` 并拒绝 3xx 后成功。新增运行时回归直接执行
真实 Provider RPC，通过内部 outbound Worker 模拟上游，能在不联网、不使用密钥的
CI 中复现并拦住这一错误。它也证明重定向不会转发凭据。

真实模型回复通过后，连续运行公共接口和聊天用例触发正常的注册限流。联调脚本
现在遵循服务端 `x-retry-after`，与原有聊天回归相同；业务限流没有放宽。

## 边界

此模式只替换文本/结构化 LLM。ASR、Embedding 仍是本地受控响应，Vectorize
仍是临时索引替身；TTS、模型原生 token 流和 WebSocket 语音协议不属于本次实现。
客户端 SSE 已实际验证，但它由现有业务入口在非流式推理完成后生成。

MiMo 不代表生产 Qwen 的输出质量或 Cloudflare 远端绑定验收。这次没有发布生产，
没有将局部联调结果声明为 CF-4/CI-1 完整资格。配置和交互运行命令见
[本地业务契约](../../../deploy/cloudflare/contracts/README.md)。
