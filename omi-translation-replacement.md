# 翻译替代调研:国内云 API + 开源自部署,无缝替换 Gemini

日期: 2026-08-09 · 目标: 4C8G 自托管部署,翻译替代 Gemini,支持不同 provider 无缝切换。

## 一、现状(代码确认)

```
业务代码 → TranslationProviderChain (utils/translation_core/providers.py:177)
  按 profile.providers 顺序尝试,失败 fallback
    ├─ GeminiTranslationProvider (provider=gemini, model=gemini-2.5-flash-lite)
    │    └─ 内部: get_llm('translation').invoke(...)  ← LangChain LLM
    └─ NllbTranslationProvider (provider=nllb, fallback)
         └─ HOSTED_TRANSLATION_API_URL (自托管 GPU,已弃用,中文弱)
```

**关键洞察 — 无缝替换的开关只有 1 行**:
`utils/llm/model_config.py:95` → `'translation': ('gemini-2.5-flash-lite', 'gemini')`
- `get_llm('translation')` 按 QoS 表返回 LLM client
- Gemini provider 内部就是 `get_llm('translation').invoke(prompt)` — **换成任何 OpenAI 兼容模型,整个翻译链路零改动**
- `OPENAI_COMPATIBLE_PROVIDERS`(utils/llm/providers.py:44)已有注册机制(openrouter 模式:base_url + api_key_env)

## 二、国内云厂商翻译 API(专用翻译接口,非 LLM)

| 厂商 | 产品 | 价格 | 免费 | 语言 | 接口 |
|---|---|---|---|---|---|
| **阿里云** | 机器翻译通用版 | **50 元/百万字符** | 100万/月 | 214 种 | Aliyun SDK |
| | 机器翻译专业版 | 60 元/百万 | 100万/月 | 更多领域词 | SDK |
| **腾讯云** | 文本翻译 | ~58 元/百万(后付费) | 500万/月 | 15 种主流 | TMT SDK |
| | 资源包 | 550元/1000万(~55元/百万) | — | | |
| **百度翻译** | 通用文本翻译 | 50元/百万字符(标准版) | 100万/月 | 200+ 种 | HTTP |
| **火山引擎** | 翻译 | 按量 | 免费额度 | 多语 | SDK |
| 对比: Google NMT $20/百万, DeepL $25/百万 | | | | | |

→ 专用翻译 API 便宜,但**接口与 Gemini 不同**(非 LLM),需写专用 provider(改动大)。

## 三、国内 OpenAI 兼容 LLM(无缝替换首选)

| 平台 | 模型 | OpenAI 兼容? | 备注 |
|---|---|---|---|
| **阿里云百炼(DashScope)** | Qwen3 系列(max/plus/flash/turbo) | ✅ **完全兼容** | 改 base_url+key+model 即可;Qwen3 支持 119 语言、翻译强、Apache 2.0 |
| **火山方舟(Ark)** | 豆包/DeepSeek | ✅ 兼容 | |
| **DeepSeek 官方 API** | deepseek-chat(V3) | ✅ 兼容 | 便宜,中英翻译强 |
| **智谱 GLM** | glm-4 系列 | ✅ 兼容 | |
| **SiliconFlow 硅基流动** | 多个开源模型 | ✅ 兼容 | 托管开源模型 |

**Qwen3 翻译能力**: 119 语言/方言,性能对标 Gemini 2.5 Pro(ArenaHard 95.6),Apache 2.0 开源,可自部署(vLLM/SGLang)或走百炼 API。

## 四、开源自部署(4C8G 可用)

| 模型 | 规模 | 4C8G 可行? | 翻译质量 | 部署 |
|---|---|---|---|---|
| **Qwen3-4B** | 4B | ✅ (量化后 ~3GB) | 强,119 语言 | vLLM/Ollama/llama.cpp |
| **Qwen3-0.6B/1.7B** | 小 | ✅ | 中英基本可用 | Ollama 极轻 |
| NLLB-200(现状) | 600M/1.3B/3.3B | ✅ | **中文弱**(基准 56-63% vs Google 100%)| CTranslate2(已有) |
| M2M-100 / MADLAD | 中 | ✅ | 中文一般 | — |

→ **Qwen3-4B(或 -1.7B)自部署是最优开源选择**:比 NLLB 中文强得多、4C8G 可跑、OpenAI 兼容(vLLM 起 `/v1/chat/completions`)。

## 五、无缝替换方案(兼容 Gemini 格式)

**方案 A(推荐,零业务改动): Qwen 替换 translation 的 LLM**
```
1. 百炼 API: OPENAI_COMPATIBLE_PROVIDERS 加 'qwen' provider
   { name: 'qwen', api_key_env: 'DASHSCOPE_API_KEY',
     base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' }
2. model_config.py:95 改: 'translation': ('qwen3-flash', 'qwen')
3. GeminiTranslationProvider 内部 get_llm('translation') 自动返回 Qwen
   → TranslationProviderChain 零改动,Gemini provider 语义保留
```
- 结构化输出 `GeminiTranslationBatch`:Qwen OpenAI 兼容支持 `response_format`/json_mode,需验证结构化输出兼容(或改用文本解析)

**方案 B: 开源自部署 Qwen3-4B**
```
1. 4C8G 跑 vLLM/Ollama: qwen3-4b,OpenAI 兼容 /v1/chat/completions
2. OPENAI_COMPATIBLE_PROVIDERS 加 'qwen-local'
   { name: 'qwen-local', base_url: 'http://127.0.0.1:8000/v1' }
3. model_config.py:95 改: 'translation': ('qwen3-4b', 'qwen-local')
```
- 零 API 成本、数据不出服务器、4C8G 可跑

**方案 C: 专用翻译 API(改动大,不推荐)**
- 阿里 50 元/百万 / 腾讯 ~58 元/百万 — 便宜但需写专用 provider(非 LLM 接口)

## 六、结论

1. **无缝替换 Gemini = 改 1 行 QoS 配置**(`model_config.py:95`)+ 注册 OpenAI 兼容 provider
2. **推荐**: 百炼 Qwen(API,零运维)或 **Qwen3-4B 自部署**(4C8G,零成本,数据本地)——都走方案 A/B
3. **NLLB 可彻底退役**: Qwen 中文远强于 NLLB(基准 56-63% vs 100% 的差距,Qwen 基本持平)
4. 结构化输出是唯一需验证点:Qwen OpenAI 兼容的 `response_format` 是否支撑 `GeminiTranslationBatch` 的 schema

## 七、结构化输出兼容性(关键验证点)

`GeminiTranslationProvider` 用 LangChain `with_structured_output(GeminiTranslationBatch)`(Pydantic schema → JSON)。

DashScope OpenAI 兼容接口支持:
- **JSON Schema 模式**: `response_format={"type":"json_schema","json_schema":{...},"strict":true}` — **仅 selected qwen-plus 模型**
- **JSON Object 模式**: `{"type":"json_object"}` — 大部分 Qwen 模型
- **注意**: 结构化输出需 `enable_thinking=false`(思考模式不兼容)

**LangChain 适配**:
- `with_structured_output()` 默认发 json_schema → 用 **qwen-plus**(支持 strict schema)最稳
- 或改为文本解析: prompt 要求输出 JSON,用 `json.loads` 解析成 `GeminiTranslationBatch`(兼容更多模型,含 qwen3-flash)

**方案 A 细化**:
```python
# utils/llm/providers.py OPENAI_COMPATIBLE_PROVIDERS 加:
'qwen': OpenAICompatibleProviderConfig(
    name='qwen',
    api_key_env='DASHSCOPE_API_KEY',
    base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
),
# model_config.py:95:
'translation': ('qwen-plus', 'qwen'),   # 或 qwen3-flash + 文本解析
```

## 验证建议

1. 加 `qwen` provider 到 OPENAI_COMPATIBLE_PROVIDERS(百炼 key)
2. 改 `model_config.py` translation → qwen
3. 跑现有翻译调用,确认 `GeminiTranslationBatch` 结构化输出正常
4. 回归: 现有 fallback 链(NLLB)不受影响
