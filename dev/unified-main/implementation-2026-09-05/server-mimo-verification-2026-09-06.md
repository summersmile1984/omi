# 本地 Server OS：MiMo 接入与实测结果

本轮把隔离的 Eddy 本地业务环境切换为 Xiaomi MiMo China Token Plan：
LLM 使用 `mimo-v2.5`，ASR 使用 `mimo-v2.5-asr`，TTS 使用
`mimo-v2.5-tts`，端点为 `https://token-plan-cn.xiaomimimo.com/v1`。
Embedding 仍是本机 Docker 的 BGE-M3/Ollama。旧 Qwen 容器已经停止，
当前后端不加载 SenseVoice/Kokoro 推理模型；本地 VAD 和音频编解码仍执行。

接入方式见 [运行说明](../../../deploy/self-host/mimo-local.md)。默认系统
提示词、工具说明、提取规则和业务存储所有者保持不变。所有本轮代码改动
均为 fork 自有文件。密钥只在仓库外的私有运行配置中，不进入源码、客户端
配置或镜像。此结果仅针对 `self_hosted.local`，不代表 Cloudflare 生产验证。

## 验证环境与方法

- 基线提交：`9b7e48dca2`；正常 Docker 构建产物：
  `memweft-contract-f37598530158-api`。
- API：`http://127.0.0.1:34880`；真实 Better Auth、PostgreSQL、Redis、
  MinIO、Qdrant、Typesense 和本地 embedding，保留原隔离验收账户及数据。
- 通过公开 HTTP/WebSocket 接口执行聊天、语音、录音和读取，使用人工
  构造的测试文字和 MiMo TTS 合成录音。不是麦克风实录，也不是 macOS UI
  验收；没有把受控模型响应算成真实推理。
- Docker 镜像来自正常后端构建；没有用源码热挂载替换业务运行容器。

## 观测结果

| 业务路径 | 真实观测 | 判定边界 |
| --- | --- | --- |
| 已有记忆聊天 | 三个新会话均调用真实记忆工具并回答已保存偏好，分别约 2.74、2.06、2.49 秒 | 检索与回答通过 |
| TTS | `/v2/tts/synthesize` 返回 24 kHz 单声道 WAV，3.52 秒，169,004 bytes | 合成通过 |
| HTTP ASR | 合成的中文录音经 `/v2/voice-message/transcribe` 返回中文转写与 MiMo 模型身份 | 转写接口通过 |
| PTT | 真实 `/v2/voice-message/transcribe-stream` 音频及 finalize，收到转写、正常关闭 1000 | 收尾通过 |
| 桌面来源录音 | `/v4/listen` 接收 9.44 秒 PCM，正常断开后完整转写入库，末段结束位置 9.44 秒 | 尾音保存通过；识别质量见下文 |
| 摘要与记忆 | 按现有桌面延迟处理流程打开详情，状态最终 completed，新偏好进入记忆库 | 处理与持久化通过 |
| 新记忆问答 | 再经公开聊天询问，正确回答刚录入的坚果偏好与每周频率，约 4.14 秒 | 写入后检索通过 |
| 录音提取提醒 | 摘要中出现打电话事项，但 `concrete_deliverable=false`；原策略返回 ignore，候选列表为空，任务列表没有新增 | 不能宣称自动任务创建通过 |

录音会话的初次 finalization 显示 stale/fenced，对应桌面原有 deferred
处理所有权；首次读取详情后才完成摘要和记忆提取。本轮没有更改此流程。
桌面自动提取本来就只产生建议，用户接受后才创建任务；本次模型给出的
具体事项判定还未达到建议准入条件。对真实提取结果运行原有
`run_capture_policy`，得到 `ignore`，与空候选列表一致。

## 本轮修复与剩余质量问题

录音收尾最初只保存前 5 秒，最后约 4.44 秒的 ASR 尚未返回就开始最终处理。
fork 适配现在等待已接收音频及原有转写持久化任务完成；正常与失败分支
均有行为回归测试，重建镜像后的真实 9.44 秒录音验证了尾段入库。

ASR 的原始响应仍观察到 `1.`、`think>`、`<chinese>` 等非语音文本，且
测试中的 Alex 被识别成 Alice。把同一完整 9.44 秒音频直接交给 MiMo，
约 1.12 秒返回，仍带 `1.` 且仍识别为 Alice；分窗路径另出现控制样式文本。
这证明异常不全由本地分窗产生，但合成发音与识别各自的影响尚未分离。
官方 ASR 文档未约定这些标记的清理规则，因此本轮保留原始识别结果，
没有添加提示词、按期待答案改写转写或任意剥除标记。ASR 质量尚未验收。

另修复了本地入口代理的 HTTP/1.0 完整响应收尾：Python 3.11.15 在已读满
Content-Length 后关闭上游 socket，继续设置 socket 超时会漏发下游最后
的 chunk。使用真实 TCP/HTTP 回归证明完整响应能够正常结束。

## 验证记录

- 正常 API 镜像内、禁用网络，通过 `backend/test.sh` 的
  `BACKEND_UNIT_TEST_FILE_LIST` 执行六个现有 fork 测试文件：
  `test_startup_contract.py`、`test_local_llm.py`、`test_speech_contract.py`、
  `test_speech_transport.py`、`test_selfhost_config.py`、
  `test_disabled_capabilities.py`；合计 **124 passed**。这是变更相关选择，
  不是全后端测试套件。
- `backend/.venv/bin/python deploy/self-host/ci/test_product.py`：
  Python 3.11.15，**11 passed**，含真实 TCP 代理及 MiMo 夹具配置。
- `backend/.venv/bin/python scripts/profiles/test_profiles.py`：**9 passed**。
- 固定版本 Python 格式检查、`git diff --check`、本轮上游文件零改动检查、
  diff 中无 MiMo 密钥检查通过。四个关键提示词/工具文件相对基线字节一致。
- 整个分支的 upstream-touch 检查仍报告既有
  `desktop/macos/docs/desktop-updates.mdx` 超预算（4 行 / 1 行）。本轮未修改
  该文件或放宽门禁，不能报告整个分支所有门禁通过。

私有原始证据位于 `/Users/macstudio/.codex/eddy-production/`：
`mimo-backend-runner-e-20260906.log`、`mimo-fixture-verified-20260906.log`、
`mimo-profiles-final-20260906.log`、`mimo-prompt-integrity-20260906.json`，以及
`server-mimo-20260906-e/` 下的录音、业务响应、策略结果和最终服务清单。
这些文件包含隔离账户的业务记录，不纳入仓库。

协议依据：[China Token Plan](https://mimo.mi.com/docs/zh-CN/tokenplan/Token%20Plan/quick-access)、
[Chat Completions](https://mimo.mi.com/docs/en-US/api/chat/openai-api)、
[ASR](https://mimo.mi.com/docs/en-US/api/audio/Speech-Recognition)、
[TTS](https://mimo.mi.com/docs/usage-guide/speech-synthesis-v2.5)。
