# Hosted operator AI: three OpenAI-compatible vendors on Server OS (2026-09-16)

状态：已合入 fork 分支（`feat/hosted-operator-ai`，PR-1/PR-2/PR-3 三个提交面）。
设计源头：`dev/ai-capability-contract.md`（契约 A + 能力客户端 B 的方向）。
本日志记录落地内容、验证证据与剩余验收边界。

## 交付内容

| 提交面 | 内容 |
|---|---|
| PR-1 契约层 | `backend/fork/operator_ai.py` 泛化：openrouter / siliconflow 代码冻结 spec + cloudflare-gateway（品牌清单公开身份派生）；`capabilities.py` 校验；`operator_chat.py`/`HostedEmbeddings` 托管 chat 与 embedding owner；`egress_policy.py` 三家主机进官方禁止清单（精确授权才放行）；`render.py --operator-ai` 四选一 + manifest schema |
| PR-2 能力客户端 | `backend/fork/hosted_speech.py`（multipart batch ASR + `/audio/speech` TTS + WAV 归一化）；`speech.windowed_socket` 共享 5 秒 VAD 窗口 owner（MiMo 与三家共用，只替换逐窗推理）；TTS transport 只认冻结 model id；`model_services.py` hosted 模式删除 `embedding`+`llm` 服务组（服务器零 AI 计算）并注入凭据 env |
| PR-3 验收 | `deploy/self-host/hosted-live-smoke.py`（`--self-check` hermetic 进 CI 车道 `fork-hosted-operator-smoke`；`--live` 四次真实调用出净化证据 JSON）；`deploy/self-host/README.md`、`backend/fork/README.md` hosted 章节；新测试进 `fork-selfhost-startup` 车道 |

不变量保持：无供应商回退（每能力单端点，CF 网关 fallback 链不启用）、无 BYOK、凭据只走 env/secret-file、profile 零凭据、bge-m3 1024 维不变（Qdrant 七个 collection 契约零改动）。

## hermetic 验证证据（本地实测 2026-09-16）

- `backend` fork 测试目录全量：**415 passed**（含新 `test_operator_ai.py` 28 例、`test_hosted_speech.py` 7 例）
- `scripts/profiles/test_profiles.py`：**14 passed**（含三家渲染冻结契约、CF 网关 manifest 派生、schema 拒绝未知名）
- `scripts/fork/test_model_services.py`：**5 passed**（hosted 裁剪 + MiMo 回归 + native local 不变）
- `scripts/profiles/check_tables.py`：upstream 等价 + 四平台生成表全 ok
- `hosted-live-smoke.py --self-check`：三家 × 四调用 fake transport 全 ok（model echo、1024 维匹配）
- 顺带修复：`fork/tests/test_startup_contract.py` 的 `child()` 继承父进程 bootstrap `_bind` 残留 env，导致与 `test_speech_contract.py` 组跑时 `TTS_PROVIDER conflicts`——stash 基线复现为既有缺陷，本分支独立 commit 修复（child 剥离 bootstrap 绑定名）

### CF 通道定稿为方案 A：账号 REST API + Workers AI（2026-09-16，dashboard 实证）

在 Cloudflare dashboard（账号 `05e70a39e7205b977404d4563e9803d7`，已有网关
`default`）确认：账户级 REST API 是当前调用面，**单个 `CLOUDFLARE_API_TOKEN`
覆盖全部四能力**，`cf-aig-gateway-id` 头把调用路由进网关；BYOK（Provider
Keys）一项都未配置，原设计的"CF token + 上游 provider key"双凭据取消。

冻结 spec（全部抄 fork CF 部署的生产模型，不猜）：

| 能力 | 模型 | 端点形状 |
|---|---|---|
| chat | `@cf/meta/llama-3.1-8b-instruct-fast` | `/ai/v1/chat/completions`（OpenAI SDK 兼容） |
| embeddings | `@cf/baai/bge-m3`（1024） | `/ai/v1/embeddings` |
| ASR | `@cf/openai/whisper-large-v3-turbo` | `/ai/run` envelope（input.audio base64） |
| TTS | `@cf/deepgram/aura-1` | `/ai/run` envelope（input.text）→ 二进制音频 |

CF 通道的 ASR/TTS wire 形状与标准 multipart 不同，`hosted_speech.py` 按
provider 分支；冒烟 fake、`model_services` 凭据注入（CF 只需
`CLOUDFLARE_API_TOKEN` 一行）、egress 授权面同步更新。hermetic 回归：415
passed + 冒烟自检三家全 ok。

## live 冒烟实测（2026-09-16，operator key）

每家四次真实调用，`backend/.venv/bin/python
deploy/self-host/hosted-live-smoke.py --live <vendor> --evidence /tmp/...`，
凭据走 env（`backend/.env` gitignored，不进仓库）。

| 供应商 | chat | embeddings | asr | tts | 证据 |
|---|---|---|---|---|---|
| openrouter | **ok** `qwen/qwen3-8b` echo, stop, 2.3s | **ok** echo `parasail-bge-m3`, **1024 维匹配**, 1.6s | **ok** `whisper-large-v3`, TTS 产物转写 23 字符, 1.9s | **ok** 单声道 16-bit WAV 3.7s | `/tmp/omi-hosted-openrouter-smoke.json` |
| cloudflare-gateway | **ok** `@cf/meta/llama-3.1-8b-instruct-fast` echo, stop, 0.8s | **ok** `@cf/baai/bge-m3` echo, **1024 维匹配**, 0.6s | **ok** `@cf/openai/whisper-large-v3-turbo`, TTS 产物转写 22 字符 | **ok** aura-1 mp3 → 归一化 16-bit 单声道 WAV | `/tmp/omi-hosted-cloudflare-smoke.json` |
| siliconflow | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | — |

### CF 通道的三处 wire 修正（2026-09-16 实测驱动）

1. **token 权限**：`/accounts/{id}/ai/*` 全部端点需要「帐户 → Workers AI → 读取」；
   只持 AI Gateway 权限的 token 返回 401 code 10000（Cloudflare 文档明文）。
   `ai-api` token 已在 dashboard 补权限：AI Gateway:运行 + Workers AI:读取。
2. **统一 envelope 丢音频**：`/ai/run`（envelope {model, input}）对 aura-1 返回
   `{result:{}}` 且无音频字节；**model-in-path 直接端点**
   `/ai/run/@cf/deepgram/aura-1` + 裸 input body（`{"text":...}` / `{"audio":base64}`）
   返回真实 audio/mpeg，且 `cf-aig-gateway-id` 头仍然生效（网关路由保持）。
   ASR/TTS 的 egress 授权面相应精确到 `/run/<model>`。
3. **base URL 双拼 bug**：初版 spec 把 asr/tts base 存成 `/ai/run` 再拼 `/run`
   → `/ai/run/run`（No route for that URI）。修正为 base 存 `/ai`，客户端拼
   `/run/<model>`，与授权面同一构造。

### live 实测推翻的两处初版冻结选型（2026-09-16 修正）

1. **OpenRouter chat 初选 `openai/gpt-4o-mini` 403**：OpenAI/Google 上游对
   operator 的地区（CN 网络）按供应商条款拒绝（403 "violation of provider
   Terms Of Service"），Google 系同样被拦。改选 `qwen/qwen3-8b`（Alibaba
   供应商直连 200）；`deepseek/deepseek-chat-v3.1`（DeepInfra）与
   `qwen/qwen3-32b`（SiliconFlow）同样实测可用，为备选。
2. **OpenRouter TTS 初选 `openai/gpt-4o-mini-tts-2025-12-15` 400**（该日期
   slug 已从 OpenRouter 语音目录下线）。改选 `minimax/speech-2.8-turbo` +
   文档 voice id `female-shaonv`（实测 200 返回 mp3；qwen-audio-3.0-tts 的
   DashScope voice 命名对网关不透明，未采用）。
3. **OpenRouter embeddings 的 echo 是路由供应商 id**（实测 `parasail-bge-m3`
   ≠ 请求的 `baai/bge-m3`）：`HostedEmbeddings` 的身份校验放宽为"精确 id 或
   以架构名结尾的路由 echo"，其余 fail-closed（回归测试覆盖）。
4. ASR 探针改喂 TTS 产物：初版对静音测试音返回空转写（wire 通但断言无意义），
   现在用真实语音往返（TTS "hosted operator smoke" → whisper 转写 23 字符）。

## 剩余验收（刻意未做，需真实栈或密钥）

1. `runtime-evidence.py` / `runtime_provider_attestation.py` 的 hosted 分支——
   现有 schema 硬性断言本地栈形状（`mlx_moss_diarize`、`generic`/`direct`），
   hosted 运行时证据要等 hosted Compose 真实起栈后接入；在无栈状态下盲改验收
   schema 会造出没有受众的检查。
2. hosted 栈的 cutover 门禁与 `zero-vendor-acceptance.sh` hosted 档——同一理由，
   依赖上一步。
3. 三家 live 实测（本文上表）——待 operator key。
