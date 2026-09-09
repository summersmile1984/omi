# Eddy Cloudflare：真实模型接口预检

2026-09-06 05:31（北京时间），通过已有 Wrangler 授权访问目标生产账户的
Cloudflare REST API。此次查询不是本机 Docker 推理，也不是受控响应。
使用当前配置的模型和合成验收数据，没有修改应用默认提示词。

| 配置模型 | 实际结果 |
| --- | --- |
| `@cf/meta/llama-3.2-3b-instruct` | HTTP 200，约 0.96 秒；简单探测返回 ready，41 input / 2 output tokens |
| `@cf/baai/bge-base-en-v1.5` | HTTP 200，约 0.70 秒；返回 `[1, 768]` 向量 |
| `@cf/openai/whisper-large-v3-turbo` | HTTP 200，约 1.67 秒；转写完整 9.44 秒合成录音 |
| `@cf/deepgram/aura-1` | HTTP 200，约 1.10 秒；返回 8,620 bytes 音频，ffprobe 确认 MP3、22,050 Hz、单声道、约 1.437 秒 |

这些结果证明该账户的上述模型 REST 接口可调用，不证明应用内的模型绑定、
默认业务提示词、工具调用、WebSocket 流式 ASR、Vectorize 检索或 macOS 端到端
链路通过。转写仍把测试人名识别为 Alice；合成发音与识别误差尚未分离。

同次 `GET /workers/scripts` 返回 HTTP 200，目标账户没有任何 `eddy-*`
Worker。因此 Eddy 生产应用仍未发布。没有执行远端 SQL、上传 Worker、
改变权限或接受新计费条款。四次推理调用的用量由现有账户承担。

私有结果：`/Users/macstudio/.codex/eddy-production/cf-real-provider-observation-20260906.json`；
原始模型响应和 TTS 音频保留在同目录 `cf-provider-*-20260906.*`。
报告不包含 API token。观测时基线为 `94302bbc30`。

协议来源：[Llama](https://developers.cloudflare.com/workers-ai/models/llama-3.2-3b-instruct/)、
[BGE](https://developers.cloudflare.com/workers-ai/models/bge-base-en-v1.5/)、
[Whisper](https://developers.cloudflare.com/workers-ai/models/whisper-large-v3-turbo/)、
[Aura](https://developers.cloudflare.com/workers-ai/models/aura-1/)。
