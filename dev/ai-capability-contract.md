# 统一 AI 能力契约:embedding / ASR / TTS(提案)

## 0. 先纠正一个前提:这是两份代码,不是一份代码的两个部署

我前一版把"统一契约"说成了"代码一样",这是错的。实测:

| | 位置 | 体量(不含测试) |
|---|---|---|
| Server OS / local dev | `backend/**` | **10,006** 个 `.py` |
| Cloudflare | `deploy/cloudflare/**` | **2,248** 个 `.py` + **176** 个 `.ts/.mjs` |

CF 侧是**独立实现**(不是镜像):`deploy/cloudflare/python/api-ai/src/*.py` 的文件名
(`chat_generation_routes.py` / `voice_transcription_routes.py` / `app_generation_routes.py` …)
与 `backend/routers/*` **不对应**,是另一套分解方式。

两个运行时的 HTTP 传输也不通用:

```python
# OS(backend/fork/embedding.py:8)      import httpx
# CF Python Worker(api-ai/src/entry.py:15)  from workers import fetch as worker_fetch
```

**跨运行时共享代码在本仓库有先例**,但目前只有 JS/TS:
`auth/shared/jwt-policy.mjs` 同时被 CF Workers(`deploy/cloudflare/workers/auth/*.ts`)和
Node 的 `auth-server` 引用;`runtime/shared/fallback.mjs` 同类。**Python 侧目前没有任何共享**。

所以"代码一样"有三种完全不同的目标,成本差一个数量级:

| 目标 | 含义 | 代价 |
|---|---|---|
| **A. 契约一样** | profile/env 契约统一,两边各自实现;调用的形状相同 | 小(本方案 §2 的契约层) |
| **B. 能力客户端一份** | 把"调用标准 AI API"这段代码做成**跨运行时共享模块**(带传输缝:`httpx` vs `workers.fetch`),两边 import 同一文件 | 中;有 `auth/shared` 先例可循 |
| **C. 产品逻辑一份** | CF 不再实现业务逻辑,变成 operator 后端的边缘层(仓库里已有 `ORIGIN_BACKEND_URL` 混合模式的痕迹) | 大:改变 CF target 的部署模型 |

**本方案只承诺 A + B**:A 让"模型可配置"成立,B 让 AI 那段代码真的只有一份。
C 是另一个量级的决策(等于重定义 CF target),"其余产品逻辑仍是两份"这件事不会被本方案改变。



日期: 2026-09-14 · 分支: `main` @ `ae075915af` · 状态: **待定方向,未实施**

目标:local dev、Server OS、Cloudflare 三端**同一套契约(A)+ 同一份能力客户端代码(B)**,
embedding / ASR / TTS 只走一种标准 API;模型与端点变成配置(profile 声明契约,env 声明部署事实),不再有 per-target 分支,**不再用 Workers AI**。

---

## 1. 今天三端各写一套(实测)

| 能力 | Server OS / local dev | Cloudflare | 分歧 |
|---|---|---|---|
| embedding | Ollama `bge-m3:latest`,**1024 维**(`deploy/profiles/self_hosted.yaml`) | Workers AI `@cf/baai/bge-m3`、`bge-base-en-v1.5`、`bge-large`、`bge-small`;**`embedding_dims: 1536`**(Vectorize 上限) | 提供方、维度、代码路径全不同 |
| ASR(批) | sherpa-onnx `SenseVoice` 文件包 或 MiMo HTTP | Workers AI `@cf/openai/whisper-large-v3-turbo`、`@cf/openai/whisper`、`@cf/deepgram/nova-3` | 本地模型 vs 绑定 |
| ASR(流式) | 进程内 sherpa + VAD(5s 窗口) | Worker 内 `ai.run` | 无统一契约 |
| TTS | sherpa `Kokoro` 文件包 或 MiMo HTTP | Workers AI `@cf/deepgram/aura-1`,`capabilities.tts_provider: workers_ai` | 同上 |
| chat/LLM | Ollama `qwen3:1.7b` 或 MiMo | Workers AI llama / qwen / glm | 同上 |

代码位置:

- OS/local:`backend/fork/{embedding,llm,speech,speaker_embedding,local_llm,mimo_speech}.py`,
  经 patch 注册表挂到上游调用点(`backend/fork/patches/{embedding,llm,speech,vector,speaker_embedding}.py`)。
- Cloudflare:`deploy/cloudflare/python/api-ai/**` 与 `deploy/cloudflare/workers/{edge,realtime,jobs}/**`,
  **约 20 个源文件**(不含测试)直接调用 `env.AI` / `ai.run(...)`。

**已经有一半地基**:`backend/fork/operator_ai.py` 的 provider 形状就是标准 API 形状
(`provider / base_url / model / asr_model / tts_model`,OpenAI 兼容 `/v1`),`fork/llm_http.py`
是有界的 HTTP 客户端。统一 = 把这个"operator 端点"从 MiMo 专用推广成**任何 OpenAI 兼容端点**,
并让三端都用它。

---

## 2. 目标契约

**一个实现,一种协议**:所有能力都通过 HTTP(S) 的标准路径,客户端代码与 target 无关。

```
embeddings   POST {AI_BASE_URL}/embeddings                {model, input[]}      → {data[].embedding, model}
ASR(批)      POST {AI_BASE_URL}/audio/transcriptions      multipart file+model  → {text, segments?}
ASR(流)      WS   {AI_BASE_URL}/audio/stream              (契约待定,见 §4.1)
TTS          POST {AI_BASE_URL}/audio/speech              {model, input, voice} → audio bytes
chat         POST {AI_BASE_URL}/chat/completions          (已是这个形状)
```

**profile 声明契约,env 声明部署事实**:

```yaml
# deploy/profiles/<target>.yaml
ai:
  contract: openai-compatible          # 唯一的实现种类
  embeddings: {model: bge-m3, dimensions: 1024}
  speech:
    stt: {mode: batch|stream, model: sensevoice, streaming_contract: ws}
    tts: {model: kokoro, voices: [af_heart, ...]}
  chat: {model: qwen3:1.7b}
```

```bash
# 每一端自己的 env(唯一允许不同的地方)
AI_BASE_URL=https://ai.example.com/v1     # 本地: http://127.0.0.1:11434/v1 之类
AI_API_KEY=...
```

不变式(可直接写成检查):

1. `backend/**` 与 `deploy/cloudflare/**` 里**不得**出现按 target 分支选 AI provider 的代码;
   选择只来自 profile/env。
2. 任何 AI 调用只经过一个客户端模块(每语言一份:Python、TS),不在业务代码里散落 fetch。
3. `deploy/cloudflare/**` 不得再出现 `env.AI` / `ai.run(`(若无 Workers AI 例外,见 §4.4)。

---

## 3. 关键发现:缝已经存在,只是被限制在本地

`deploy/cloudflare/contracts/` 里已经有一个**与 Workers AI binding 同接口**的 HTTP 实现:

```
contracts/provider-dev.ts
  export class Provider extends WorkerEntrypoint {
    async run(model, input, options?) {          // ← 与 ai.run 完全同签名
      if (model === "@cf/baai/bge-m3")  return runDevEmbedding(env, input);   // Ollama /api/embed
      if (typeof input.audio === "string") return runDevAsr(env, input);      // MiMo ASR
      if (typeof input.text === "string")  return runDevTts(env, input);      // MiMo TTS
      return runDevLlm(env, input);                                           // MiMo LLM
    }
  }

contracts/local-config.mjs:184-198
  config.services.push({ binding: "AI", service: `${namespace}-provider`, entrypoint: "Provider" })
  // "Selected only by the disposable local target. No production config imports it."
```

`contracts/dev-media.mjs` 甚至已经强制了与 OS 一致的契约:embedding 必须是 loopback Ollama、
模型必须是 **bge-m3(1024 维)**,否则报错。

**结论:统一不是"重写 20 个调用点",而是"把这个 Provider 从 dev 专用提升为唯一实现"**:

- 调用点保持 `ai.run(model, payload)` 不变 → **~20 个调用点、响应解码器、~15 个测试文件、
  5 个 live contract 全部不用改**(Provider 已经返回 Workers AI 的响应形状)。
- 要改的是**实现与配置**:4 个 binding 声明改为指向 provider 服务;`dev-media.mjs` 从
  "MiMo 硬编码"泛化成"可配置的 OpenAI 兼容端点";26 个 `@cf/...` 字面量变成配置;
  实时流式需要 WS 能力的 provider(见 §4.1)。
- 仓库里已有两个可移植 HTTP 先例:`/v1/stt/transcribe` → `ASR_API_BASE_URL`(批 ASR),
  `/v1/ai/{path}` → `AI_API_BASE_URL`(通用 OpenAI 兼容代理,staging 上 fail-closed)。

### 顺带修掉一个现存不一致

Cloudflare 今天同时存在**三个维度数字**:

| 位置 | 模型 | 维度 |
|---|---|---|
| 公开 `/v1/embeddings`(`api-ai`) | `@cf/baai/bge-base-en-v1.5` | 768 |
| 记忆/向量路径(`api-core`、`jobs`) | `@cf/baai/bge-m3` | 1024(Vectorize 索引硬断言 1024) |
| `deploy/profiles/cloudflare.yaml` | — | `embedding_dims: 1536` |

而 OS 是 bge-m3/1024、本地 dev provider 也强制 1024。统一后这些收敛成一个值。

### 改造面(按文件)

| 区域 | 代表文件 | 改法 |
|---|---|---|
| **CF provider(核心)** | `deploy/cloudflare/contracts/{provider-dev.ts,dev-media.mjs,dev-llm.mjs}` | 提升为正式实现;端点/模型/密钥改为配置 |
| CF binding 声明 | `python/api-ai/wrangler.jsonc:9-11`、`api-core:12-14`、`jobs:131`、`realtime:13` | `ai` binding → provider 服务绑定 |
| CF 配置 | `wrangler.jsonc` 里的 26 个 `@cf/...` 字面量、`workers/shared/provider-policy.ts:14-21` | 模型 id 变为 profile/var;流式策略不再硬编码 |
| CF 调用点/解码/测试 | `python/api-ai/src/**`、`workers/**`(≈20 处 `ai.run`)、~15 测试、5 live contract | **不动**(接口与响应形状保持) |
| OS/local provider | `backend/fork/{embedding,speech,llm,local_llm,mimo_speech}.py` | 收敛成"标准客户端 + profile 选择",与 CF provider 同一契约 |
| profile/render | `deploy/profiles/*.yaml`、`scripts/profiles/render.py`、`check_tables.py` | 增加 `ai` 段并让 CF 真正读它(今天 CF 完全不读 profile) |

## 3.2 OS / local 侧:机制完全不同,而且 Ollama 是硬校验

OS 不是"按 env 选 provider",而是**导入期替换模块符号**:
`backend/fork/registry.py:93 setattr(module, patch.attribute, patch.build(original))`,
由 `applies_to=lambda row: row.get('target')=='self_hosted'` 驱动(`bootstrap.py:162` API / `:193` MEMORY_MAINTENANCE)。
三个能力三个独立 owner:

| 能力 | owner | 实现 | 是否标准 API |
|---|---|---|---|
| embedding | `fork/embedding.py`(`OllamaEmbeddings`) | **Ollama 原生** `GET /api/tags`、`POST /api/show`、`POST /api/embed` | ❌ 原生,无 key |
| LLM | `fork/local_llm.py`(`LocalChatModel`) | **Ollama 原生** `/api/chat` | ❌ 原生 |
| STT(流式+批) | `fork/speech.py` + `utils/sensevoice/**` | **进程内 sherpa-onnx**(`model.int8.onnx` + `tokens.txt`) | ❌ 无 HTTP 面 |
| TTS | `fork/speech_transport.py` + `fork/speech.py` | **进程内 Kokoro**(`sherpa_onnx.OfflineTts`)+ ffmpeg 转 mp3 | ❌ 无 HTTP 面 |

**硬耦合(必须改掉才能"模型可配置")**:

- `fork/model_contract.py:42-44`:`if value['provider'] != 'ollama': raise ValueError('self-host model requires the explicit Ollama provider')`
  —— embedding 与 LLM **都**强制 `provider: ollama`。
- 还固定 `runtime_version == '0.33.3'`、`kv_cache_type == 'q8_0'`;artifact 身份 = **Ollama 仓库布局**
  (`manifests/registry.ollama.ai/<ns>/<name>/<tag>` + `blobs/sha256-*`)。
- 因此"换个模型"今天 = 换一个 Ollama tag,而不是换一个端点。

**但没有耦合 Ollama 的部分**:整条语音路径(sherpa-onnx + 内容寻址文件库)与 MiMo 路径
(`ChatOpenAI` + `/v1/chat/completions`)。也就是说**语音本来就不是 HTTP,而是进程内模型调用**。

**OS 侧已经存在的标准 API 形状**(可直接复用):
`utils/llm/gateway_client.py` 的 `POST {OMI_LLM_GATEWAY_URL}/v1/embeddings`(bearer token)、
`fork/mimo_chat.py` 的 `ChatOpenAI(base_url=...)`、`fork/mimo_speech.py` 的 `/v1/chat/completions`(音频走 content block)。

**profile schema 的不对称(要先修)**:`scripts/profiles/render.py:162-180` 规定
**self_hosted 不得自己声明** `embedding_dims` / `stt_providers` / `tts_provider` / `llm_provider`
(这些由 `llm`/`embedding`/`speech` 能力行推导);而 `cloudflare.yaml` 恰恰声明了这些,CF 又完全不读。
统一 schema 必须二选一,不能两边都成立。

**会跟着改的测试**(实测清单):`backend/fork/tests/test_embedding.py`(断言 `/api/tags|show|embed`)、
`test_local_llm.py`、`test_speech_contract.py`、`test_speech_transport.py`、`test_startup_contract.py`、
`test_selfhost_config.py`、backend 的 `test_tts*.py`/`test_mimo_*.py`,
以及 `deploy/self-host/runtime_provider_attestation.py`(TTS 路由形状
`{provider, model, transport, endpoint_origin}` 与 `validate_tts_probe_identity`)。

## 4. 需要你拍板的三件事(现在多了一条)

### 4.1 流式 ASR 的契约(决定能不能真的统一)

标准 API 里 **batch** 转写有事实标准,流式没有。选项:

- **(a)** 只标准化 batch;流式仍走"本机进程内"(那么 CF 无法统一流式,只能批处理);
- **(b)** 自定义一个 WS 契约(形状照 OpenAI Realtime:`session.update` / `input_audio_buffer.append` / 转写事件);
- **(c)** 伪流式:客户端按 VAD 切片,逐片 `POST /audio/transcriptions`。

推荐 **(b)**,但需要一个本地实现(可用 faster-whisper/sherpa 包一层)。**不选它,CF 的实时语音就不可能和 OS 同代码。**

### 4.2 向量维度与索引迁移

统一模型后维度由模型决定(现在 OS 1024 vs CF 1536)。CF 的 Vectorize 索引维度建好后不可改 →
要么选一个 1024 维模型并把 CF 索引重建(需要数据重投影),要么允许 profile 声明维度并让两端各自建索引。
**这是数据迁移,必须你确认。**

### 4.3 模型溯源强度

OS 今天用 digest 固定 artifact(供应链强度高,`model_contract.py`)。改成"信任端点"会丢掉这层。
建议保留一档:**端点必须自报 model id,且 profile 可声明期望 digest/版本**,不匹配则 fail-closed。
要不要保留这层,决定 profile schema 的复杂度。

### 4.4 语音要不要也变成"标准 API 服务"

今天 embedding/LLM 是 **HTTP(Ollama 原生)**,而 STT/TTS 是**进程内模型**。统一成标准 API 有两条路:

- **(i) 保留进程内实现**,只把"选择与契约"统一(代价小,但 CF 无进程内模型 ⇒ 三端仍不同);
- **(ii) 把 sherpa/Kokoro 包成本机一个 OpenAI 兼容服务**(`/v1/audio/transcriptions` + `/v1/audio/speech`),
  三端只认 HTTP(代价:多一个本地服务与一份镜像)。

选 (ii) 才真正做到"代码一样";选 (i) 等于承认语音在 OS 上是特例。

### 4.5 Workers AI 是否 100% 退役

除了这三项,CF 还在用 Workers AI 做图像(`flux-1-schnell`、`uform-gen2`)、翻译(`m2m100`)等。
"不用 worker ai" 是只针对 embedding/ASR/TTS,还是全部能力?全退的话改造面翻倍。

---

## 5. 分期(每期独立可验证)

| 期 | 内容 | 验证 |
|---|---|---|
| P0 | 契约与 profile schema 定稿(不改行为) | `scripts/profiles/test_profiles.py`、`check_tables.py` 通过;文档进 `docs/doc/developer/` |
| P1 | **embeddings 统一**:把 CF 的 `AI` binding 指向 Provider、模型改配置、维度收敛到 1024 | 公开 `/v1/embeddings` 与记忆路径同模型同维度;OS 与 CF 打同一契约 |
| P2 | TTS 统一(Provider 已实现 MiMo TTS,改配置即可) | 同一输入同一 wav;**退役 `@cf/deepgram/aura-1`** |
| P3 | batch ASR 统一 | 同一 wav → 同形结果;**退役 whisper/nova 绑定** |
| P4 | 流式 ASR(取决于 §4.1) | 同一条 WS 契约在两端跑通 |
| P5 | chat/LLM 及其余 Workers AI 用途 | 取决于 §4.4 |

**建议从 P1 开始**:改动最小、可回滚、立刻能让 local dev 与 CF 走同一条代码路径,也为 P2/P3 定下客户端形状。

### 5.1 P1 已经实测可行(2026-09-14,本机)

Ollama 本身就提供 OpenAI 兼容端点,所以本地侧**不需要新服务**:

```
POST http://127.0.0.1:11434/v1/embeddings  {"model":"bge-m3:latest","input":"hello world"}
  → model=bge-m3:latest  dimensions=1024        ← 与 profile 声明的 1024 一致

POST http://127.0.0.1:11434/v1/chat/completions  {"model":"qwen3:1.7b", ...}
  → content="ready", message.reasoning 字段存在
```

注意两点,都要进契约:

1. **reasoning 模型需要输出预算**:同一个 prompt 给 `max_tokens: 16` 时 content 为空
   (推理 token 吃掉了预算),给 400 才得到 `ready`。契约要规定 reasoning 字段是忽略还是透出,
   以及 profile 的 `max_output_tokens` 下限。
2. 维度是**模型属性**(bge-m3=1024),所以 `embedding_dims` 必须由 profile 的模型声明推导,
   不能再由 target 硬写(CF 现在硬写 1536)。

---

## 6. 与现有材料的关系

- 这份提案**不替换** `dev/ci-cd-three-stages.md`(那是交付主线);它是那条主线里
  "三端同形"不变量的具体化。
- `contracts/deployment/README.md` 已写明:core slice 用 **HTTP embedding fixture** 且生成
  speech-disabled profile —— 说明"标准 HTTP + 可关能力"的方向仓库已经在用,本提案是把它变成唯一路径。
