# 外部依赖可调用性审计台账

日期: 2026-08-10 · 分支: feature/cloud-neutral-shim · 目标: 确认 cloud-neutral 栈所有外部接口 / AI 功能可调用

## 审计结论

所有核心外部依赖与 AI 功能**均可调用**,共发现并修复 2 个配置缺口(deploy-local.sh 未注入
`AUTH_JWKS_URL` / `AUTH_PROVIDER`,及 `SENSEVOICE_MODEL_DIR`+`STT_SERVICE_MODELS`+`BUCKET_*`)。
修复后完整 env 后端:健康检查 200、认证签发+验证 200、SenseVoice 三种语言全落本地、MinIO 9 桶自动创建。

## 冒烟结果

| 依赖面 | 配置 | 冒烟验证 | 结果 |
|---|---|---|---|
| **认证** (BetterAuth) | `AUTH_PROVIDER=better_auth` + `AUTH_JWKS_URL=http://127.0.0.1:3000/api/auth/jwks` | auth-issue 签 EdDSA JWT(329B)→ 后端 `/v1/users/me/subscription` 200(非 401) | ✅ |
| **聊天** (DeepSeek Anthropic) | `CHAT_PROVIDER=deepseek` + `CHAT_MODEL=deepseek-v4-flash` + `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` | Anthropic SDK → `[ThinkingBlock, TextBlock]` reply=`PINEAPPLE` | ✅ |
| **翻译** (DeepSeek) | `TRANSLATION_PROVIDER=deepseek` + `TRANSLATION_MODEL=deepseek-v4-flash` | `translate_text('zh', 'Hello world...')` → dest=zh, 输出 `你好，世界，你今天怎么样？` | ✅ |
| **live STT** (SenseVoice 本地 CPU) | `STT_SERVICE_MODELS=sensevoice` + `SENSEVOICE_MODEL_DIR=/tmp/sherpa/...` | `get_stt_service_for_language` → zh-CN/en-US/ja-JP 全落 `sensevoice`(先前真实音频 CER 7.81%) | ✅ |
| **存储** (MinIO) | `STORAGE_BACKEND=minio` + `MINIO_ENDPOINT` + `BUCKET_*` | `upload_profile_audio` → `get_user_has_speech_profile`=True → delete | ✅ |
| **存储桶** (MinIO 9 桶) | `BUCKET_SPEECH_PROFILES` 等 9 个 | `bucket(name)` 自动创建全部 9 桶 | ✅ |
| **队列** (Redis) | `QUEUE_BACKEND=redis` + `REDIS_DB_HOST/PORT` | `enqueue_sync_job` → SADD 去重 → worker BLPOP → POST handler(403 因缺 Cloud Tasks OIDC,预期边界) | ✅ |
| **批 STT** (MOSS) | `STT_PRERECORDED_MODEL=moss` + `MOSS_API_KEY` | 先前验证分离 S01/S02(需真实 key) | ⚠️ 可选增强 |
| **live STT** (MiMo-compatible) | `STT_SERVICE_MODELS=mimo` + `MIMO_API_KEY` + explicit operator `MIMO_API_BASE` | **真实转写成功**(2026-08-10):`你好，世界，这是测试。`; vendor endpoint 不再自动选择 | ✅ |
| **TTS** (MiMo-compatible) | `TTS_PROVIDER=mimo` + `MIMO_API_KEY` + explicit operator `MIMO_API_BASE` | **真实合成成功**(2026-08-10):`/v2/tts/synthesize` → 200,24kHz 2.7s WAV(冰糖音色)；vendor endpoint 不再自动选择 | ✅ |
| **说话人识别** | MOSS 识别 + 本地 wespeaker | 调研完成,未接默认链路 | ⚠️ 可选增强 |

## 修复的配置缺口

### 1. deploy-local.sh 未注入认证 env(2026-08-10 修复)

- **症状**: 完整 env 后端 `/v1/users/me/subscription` → 401
- **根因**: `AUTH_PROVIDER`/`AUTH_JWKS_URL` 未导出,shim 用默认 `http://127.0.0.1:3000/jwks`(404),而 auth-server 的
  JWKS 实际在 `/api/auth/jwks`
- **修复**: deploy-local.sh 增加
  ```bash
  export AUTH_PROVIDER="${AUTH_PROVIDER:-better_auth}"
  export AUTH_JWKS_URL="http://127.0.0.1:${AUTH_PORT}/api/auth/jwks"
  ```

### 2. deploy-local.sh 未注入 SenseVoice + MinIO bucket env(2026-08-10 修复)

- **症状**: 无 `STT_SERVICE_MODELS` → streaming STT 默认落 modulate(无 key → 运行必失败);无 `BUCKET_*` → 存储 shim disabled
- **根因**: 之前这些 env 靠外部注入,deploy-local.sh 未固化
- **修复**: deploy-local.sh 增加
  ```bash
  export SENSEVOICE_MODEL_DIR="${SENSEVOICE_MODEL_DIR:-/tmp/sherpa/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17}"
  export STT_SERVICE_MODELS="${STT_SERVICE_MODELS:-sensevoice}"
  export BUCKET_SPEECH_PROFILES=... # + 其余 8 个 BUCKET_*
  ```

## 环境说明

- 验证后端: 完整 env 集合 :8102 → 全绿后清理;生产实例 :8100 未动
- 真实 key: DEEPSEEK_API_KEY / ANTHROPIC_API_KEY(指向 api.deepseek.com)注入运行进程
- 模型目录: `/tmp/sherpa/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`(model.int8.onnx + tokens.txt)
- **MiMo endpoint**: 必须由 operator 显式配置 `MIMO_API_BASE`（或显式的 `MIMO_TOKENPLAN_BASE`）；服务不会根据 key 前缀选择 vendor authority，已知 vendor authority 会在 client 构造前拒绝。

## 复跑命令

```bash
# 完整验证后端(一次性)
dev/deploy-local.sh                       # 全栈启动
curl -s http://127.0.0.1:8100/health      # health
# 认证
TOKEN=$(curl -s -X POST http://127.0.0.1:3000/auth-issue -H 'Content-Type: application/json' -d '{"uid":"x"}')
curl -s http://127.0.0.1:8100/v1/users/me/subscription -H "Authorization: Bearer $TOKEN"
# SenseVoice select
SENSEVOICE_MODEL_DIR=... STT_SERVICE_MODELS=sensevoice .venv/bin/python -c "from utils.stt.streaming import get_stt_service_for_language; print(get_stt_service_for_language('zh-CN'))"
```
