# Server OS：真实模型验收环境与录音入口

本包补齐共用产品验收的运行条件：原来的 Server 夹具显式关闭语音和 LLM，
入口代理只转发 HTTP。现在可选择完整模型 profile，通过同一个隔离入口执行
HTTP 和 WebSocket。此包尚未实现 `CI-1` 完整发布执行器，也未部署 Eddy Cloudflare。

## 实现边界

- `deploy/self-host/ci/product.py` 接受完整的 embedding/LLM/speech 模型目录组；
  保留正式渲染的 profile、Compose 模型服务、只读工件、运行时和启动校验。
  不修改上游源码、模型或模型配置，不用预设 transcript 替代真实识别。
- 默认 core 模式继续使用原来的禁用语音/LLM 配置和受控 embedding。
  本包没有把 core8 的成功范围改成完整录音验收。
- 固定入口只连接本项目的 Auth/API，真实上游握手决定接受或拒绝。
  双向有界转发保留 HTTP parser 已缓冲的首帧、PCM、控制帧和关闭。
- 容量准入读取 Compose 的两个 4 GiB 模型上限，再保留 4 GiB 给应用、状态
  服务和引擎；读取 Docker 实际内存，在构建/启动前拒绝不足分配。
  这属于夹具的启动条件，不是新的业务兼容层或服务端容量承诺。
  它对应下述实测 OOM，测试通过现有 fixture command 边界提供失败机器的
  内存值，断言拒绝发生在容器启动前，并覆盖原 core 模式。

## 首轮实际运行

目录：`/Users/macstudio/.codex/eddy-production/server-real-model-20260905-a`。
唯一 Compose 项目：`memweft-contract-bcd9d7cf4ffb`，API/Auth 为本机
34880/34881。该品牌 ID 为 Eddy 的工程夹具仍使用 `Product Fixture` 展示名称，
不是 Eddy 生产品牌构建。

使用复用基础镜像 `memweft-contract-5a4bb687fc3e-base` 前，实际检查所有已跟踪
backend Python 文件与工作树 SHA-256 一致；随后正常构建 Auth、API/profile 和
`Dockerfile.llm`。没有挂载修改后的应用源码。模型目录来自既有正式 provision 工件：

| 模型 | 实际证据 |
| --- | --- |
| BGE-M3 | 正式 artifact-check 校验 3 个 blob，通过 |
| Qwen3 1.7B | 正式 artifact-check 校验 5 个 blob；profile 生成的 Ollama 服务启动并通过 API 模型准入 |
| SenseVoice/Kokoro | 完整 bundle 校验、启动实际 TTS→ASR 检查；公开 `/v2/tts/synthesize` 返回 200 |

正常 Auth/PG/Qdrant 迁移和应用 readiness 通过；共用
`contracts/deployment/core.py` 的 8 个 HTTP 用例全部通过。
测试账户经正式 TTS 生成“jasmine tea”相关材料，转成 16kHz mono PCM 后
通过 `/v4/listen` 的 Bearer Upgrade 发送。实际识别返回目标词，HTTP 读回转写，
分配说话人及提交最终化成功；队列状态依次观察到 queued、leased。

最终化未完成。北京时间 **22:03:48**，Docker VM 内核明确记录
`llama-server invoked oom-killer`、`global_oom` 和
`Out of memory: Killed process 75219 (llama-server)`，匿名 RSS 为
1,868,364 KiB。该引擎实际分配为 8,318,562,304 bytes；全局 OOM 后
Docker API 及测试 HTTP 连接失去响应，录音探针以 `httpx.RemoteProtocolError`
退出。不能据此声称已生成规范记忆、检索回答、完整导出或通过双目标闭环。

原始证据位于 `/Users/macstudio/.codex/eddy-production/`：
`server-real-model-core.log`、`server-real-model-recording.log`、
`server-real-model-oom.json` 和运行目录内的 `command-*.log`、
`runtime-source-hashes.json`、`fixture-scope.json`。账户和音频均为本次生成。

## 故障恢复与容量拒绝

夹具 owner 收到 SIGTERM 后，日志读取及 Compose 清理先后超时；停止该项目
LLM 容器的独立请求也超时。重启 Docker Desktop 恢复引擎后，实际 inspect
确认模型容器 `exited exit=137 oom=true`，随后该随机项目的所有容器、卷和网络
经同一 Compose 定义清理成功。没有删除其他项目资源。

容量准入随后在本机真实 CLI 执行：
`server-model-capacity-20260905-c/model-capacity.json` 报告实际 7.75 GiB、
要求 12 GiB、`admitted: false`，在构建/容器启动前退出。第一次实现误将
Compose JSON 的十进制字符串当作整数，`server-model-capacity-20260905-b`
记录该失败；按实际 `command-01.log` 的两个 `"4294967296"` 值修正后重跑 c。

8 个本地 runner 测试通过，包含真实 TCP 握手/首帧/100 KiB 二进制传输/401、
不完整模型目录与容量拒绝。所有测试经现有 `product.sh` local/CI 入口发现。
这些测试不代替在足够容量环境重新执行完整录音路径。

## 第二轮真实模型录音与剩余失败

Docker 分配提高到 16 GiB 后，运行目录 `server-real-model-20260905-d`、
项目 `memweft-contract-60d8eed8a542` 经容量准入、实际工件检查及正常迁移启动。
8 个公共 HTTP 用例再次通过。沿用第一轮真实 TTS 生成的合成音频，经实际
SenseVoice 识别和 Qwen 推理，最终化任务以 `completed` / `success` 结束，
`attempt_count=1`、`task_retry_count=0`、`fanout_status=completed`。

API 读回 2 条记忆，其中一条包含录音中的 jasmine 偏好，并读回 1 个任务。
该结果证明本轮录音、转写、持久化和派生处理通过，不代表以下步骤通过：

- `/v2/messages` 返回 HTTP 200 流，但缺少成功结束帧。实际异常为
  tiktoken 首次读取 `cl100k_base.tiktoken` 时尝试连接远端词表；隔离网络
  拒绝 DNS。普通 HTTP 状态成功不能代替业务完成。
- `/v1/users/export` 返回 HTTP 500。`data_export.py` 读取保留的集合时，
  PostgreSQL 迁移注册表缺少对应物理表，抛出 `SchemaNotCurrent`。

原始私有证据为 `server-real-model-core-d.log`、
`server-real-model-recording-d.log`、`server-real-model-retrieval-d.log` 和
`server-real-model-export-error-d.log`。仍未取得完整聊天/导出或 CI-1 资格。

9 月 6 日继续检查时，夹具父进程仍存活，但 Docker 明确显示该项目容器已退出；
API/入口退出时间为 `2026-09-05T19:22:28Z`，`OOMKilled=false`。
此状态不能视为模型仍在执行，也不能将本轮退出归因于先前的 OOM。

## 9 月 6 日：旧 token 计数器的离线资源修复

正式 fork Dockerfile 和验收镜像均在构建阶段执行上游已有的
`scripts/prewarm_tiktoken_cache.py`。锁定 tiktoken 自身校验词表 hash，
结果放在固定、只读的 `/opt/tiktoken-cache`。不放开应用出站网络。
现有 product CI 入口在启动服务前执行真实镜像的冷进程回归检查：
`docker run --read-only --network=none --user=10001:10001 ...`，对中英文
文本执行实际 encode/decode；词表缺失会直接失败。

本次重新构建的验收镜像 `memweft-contract-2ec66c5e3867-api` 通过该检查，
profile 与 d 轮逐字节一致。正式 `deploy/self-host/Dockerfile` 也独立构建为
`eddy-self-host-tokenizer-20260906-b` 并通过同样的断网检查。
首次正式构建缺少自托管端点而被 renderer 拒绝；第二次明确提供工程夹具
manifest 和 local stage 后成功，没有跳过渲染或将此称为生产发布。

d 轮原有存储卷未重建，恢复后换用新应用镜像，实际会话换票返回 200，
2 条已存记忆仍可读回。聊天不再报告词表下载失败，但入口仍在长响应期间
断开：API 容器保持 healthy，入口对 HTTP 使用 30 秒超时并先缓冲整条响应。
这项转发问题及导出 schema 问题仍待修复，不能报告完整聊天闭环通过。

本次夹具测试 8 项、启动配置测试 7 项通过。新增检查直接运行在现有
`deploy/self-host/ci/product.sh` 的镜像构建路径内，覆盖本记录中的实际故障；
未新增独立检查或修改上游代码、词表算法及依赖锁。

**计数更正（9 月 6 日）：** 早期探针对 `/v1/action-items` 的响应直接求
`len()`，误把 `action_items`、`has_more`、`truncated` 三个 envelope 字段
计成 3 个任务。重新读取实际 `action_items` 列表和完整导出，两者均为 1 条，
ID 一致。原始日志保留用于审计，本记录和后续探针已修正。完整导出修复及
实测边界见 [v8 导出验收](server-export-v8.md)。

**术语与适配边界更正：** 上述 tiktoken 是上游应用层的长度估算辅助，
不是中文语义分词服务，也不负责生成回答。`utils.retrieval.agentic` 在发出
模型请求前用它决定聊天历史预算；`utils.retrieval.rag` 用它判断长录音是否
先提取片段。模型仍接收文本 messages，并在 Ollama/Qwen 内部执行实际编码。
应用层词表下载不属于 Qwen 的运行要求，更不代表调用 GPT-4 生成回复。

当前上游计数器固定为 GPT-4 的 `cl100k_base`，历史预算默认 120,000；
当前自托管 profile 则是 Qwen3 1.7B，context window 8,192、最大输出 2,048。
因此预装资源仅消除了断网异常，没有完成模型特定的上下文预算适配。
这仍是需解决的边界；不能用该旧估算器宣称精准计数，也不能直接删除所有
输入长度控制来让验收变绿。
