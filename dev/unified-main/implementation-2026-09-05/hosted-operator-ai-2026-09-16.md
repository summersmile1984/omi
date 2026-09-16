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

## live 冒烟（待三家 key，`NOT_RUN` 记录）

每家四次真实调用（chat / bge-m3 embeddings / 短音频 batch ASR / 一句 TTS），
`backend/.venv/bin/python deploy/self-host/hosted-live-smoke.py --live <vendor>
--evidence /tmp/omi-hosted-<vendor>-smoke.json`。凭据放 env（不进仓库）：
`OPENROUTER_API_KEY` / `SILICONFLOW_API_KEY` /（CF 通道）
`CLOUDFLARE_GATEWAY_PROVIDER_API_KEY` + `CLOUDFLARE_API_TOKEN`。

| 供应商 | chat | embeddings | asr | tts | 证据 |
|---|---|---|---|---|---|
| openrouter | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | — |
| cloudflare-gateway | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | — |
| siliconflow | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | — |

模型 id 冻结依据（2026-09-16 官方目录核实）：OpenRouter `openai/gpt-4o-mini` /
`baai/bge-m3`(1024) / `openai/whisper-large-v3` / `openai/gpt-4o-mini-tts-2025-12-15`；
SiliconFlow `Qwen/Qwen3-32B` / `BAAI/bge-m3` / `FunAudioLLM/SenseVoiceSmall` /
`FunAudioLLM/CosyVoice2-0.5B`；CF 网关 OpenAI 路径 `gpt-4o-mini` /
`gpt-4o-mini-transcribe` / `gpt-4o-mini-tts`，embeddings 走 Workers AI
`@cf/baai/bge-m3`(1024)。live 实测若某 id 404，改冻结 spec 并重跑 hermetic。

## 剩余验收（刻意未做，需真实栈或密钥）

1. `runtime-evidence.py` / `runtime_provider_attestation.py` 的 hosted 分支——
   现有 schema 硬性断言本地栈形状（`mlx_moss_diarize`、`generic`/`direct`），
   hosted 运行时证据要等 hosted Compose 真实起栈后接入；在无栈状态下盲改验收
   schema 会造出没有受众的检查。
2. hosted 栈的 cutover 门禁与 `zero-vendor-acceptance.sh` hosted 档——同一理由，
   依赖上一步。
3. 三家 live 实测（本文上表）——待 operator key。
