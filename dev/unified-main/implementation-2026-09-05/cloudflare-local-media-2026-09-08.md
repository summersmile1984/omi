# Cloudflare 本地 MiMo 语音与 Ollama 向量实测 — 2026-09-08

Wrangler 本地开发已接通以下真实服务；这是同日 LLM 联调记录之后的新增验证。

| 功能      | 服务与模型                                           |
| --------- | ---------------------------------------------------- |
| LLM       | MiMo China Token Plan，`mimo-v2.5`                   |
| ASR       | 同一 MiMo 套餐，`mimo-v2.5-asr`                      |
| TTS       | 同一 MiMo 套餐，`mimo-v2.5-tts`，`mimo_default` 音色 |
| Embedding | 本机 Ollama，`bge-m3`，1,024 维                      |

MiMo 走用户的云端 API，Embedding 走 `127.0.0.1:11434/api/embed`；
本次没有启动 Docker AI 容器。生产 Worker 配置和默认提示词没有修改。
Python/TypeScript 业务仍通过 `env.AI.run()` 调用，由本地 Provider RPC 转接。

## 实际验证结果

完整产品联调命令退出 0，26 项全部通过：

| 验证范围                                                             | 结果      |
| -------------------------------------------------------------------- | --------- |
| 公共 HTTP 身份、记忆、任务、账号隔离等                               | 16 项通过 |
| 实际 MiMo 聊天、D1 历史、账号隔离、结构化建议、清空历史              | 5 项通过  |
| 桌面 `/v1/tts/synthesize` 返回可解码 MP3                             | 通过      |
| 移动端 `/v2/tts/synthesize` 返回可解码音频                           | 通过      |
| 将生成的语音提交 `/v1/stt/transcribe-workers-ai`，识别出工作笔记内容 | 通过      |
| `/v1/embeddings` 返回三组 1,024 维向量，相关文本相似度高于无关文本   | 通过      |
| 实际 PCM → `/v4/listen` → MiMo ASR → D1 保存 → 公共接口重读转写      | 通过      |

组件回归：`npm test` 133 个文件、1,105 项通过；`npm run typecheck` 通过。
适配器单元测试及真实 workerd RPC 测试位于现有 Vitest CI 测试目录，
覆盖响应格式、错误、重定向、向量维度、音频分段、断开取消及 PCM8 转换。
这些 CI 测试不访问真实模型；真实服务联调由下面的显式参数启用。

## 复现与证据

从仓库根目录运行；机器需要已安装的 Node 22、锁定的 Cloudflare npm/Python
依赖、本地 Ollama BGE-M3，以及 `ffmpeg` / `ffprobe`：

```sh
npm --prefix deploy/cloudflare run test:product -- \
  --llm-dev-vars /Users/macstudio/.codex/eddy-production/cloudflare-mimo.dev.vars

npm --prefix deploy/cloudflare test
npm --prefix deploy/cloudflare run typecheck
```

本次使用 `api-core/.venv/bin/python`，通过 `PYTHON` 指定；
`CLOUDFLARE_PYODIDE_CACHE_DIR` 指向既有的私有 Pyodide 缓存。
配置文件权限为 0600，未写入仓库。字段和持续运行命令见
[本地业务契约](../../../deploy/cloudflare/contracts/README.md)。

- 回归报告、测试日志及两份生成音频：
  `/Users/macstudio/.codex/eddy-production/cf-live-media-evidence-20260908/`
- 实际运行目录：
  `/var/folders/v5/vwp83hn54v75tx3l7kqjdp7w0000gn/T/memweft-cloudflare-product.WyC0jb/target/`
- `core-results.json` 记录 16 项；`chat-results.json` 记录聊天及媒体 10 项。
- 全部修改及未跟踪文件的 MiMo 密钥扫描通过。

## 本次修正及验证边界

公共 Embedding 接口原默认请求 768 维 BGE base，与本地 Ollama/记忆索引的
BGE-M3 不同；现在仅在本地目标投影中统一选择 BGE-M3。生产默认值未改动。
PCM8 输入按既有 Realtime 协议作为无符号数据转换，回归覆盖静音中心值 128。

MiMo ASR 使用 HTTP 音频识别。开发桥接接受单声道 PCM8/PCM16，按最长五秒
分段，短尾在 350ms 无新音频时提交；每会话最多一个在途请求、20 秒排队音频。
这不是 MiMo 原生实时流或说话人分离。客户端必须等最后转写到达后再断开；
断开会取消在途识别。Opus 和立体声未接入此开发桥接。

TTS 返回 MiMo 原生 MP3；不模拟 Aura/ElevenLabs 的音色身份、采样率或码率参数。
真实向量已验证，但本地 Vectorize 仍为临时索引替身。这些报告明确标记
`release_qualified: false`，不能代表远端 Cloudflare 绑定或生产发布验收。

接口依据：[MiMo ASR](https://mimo.mi.com/docs/en-US/api/audio/Speech-Recognition)、
[MiMo TTS](https://mimo.mi.com/docs/en-US/api/audio/tts)、
[Ollama Embed](https://docs.ollama.com/api/embed)。
