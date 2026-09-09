import { devLlmConfig } from "./dev-llm.mjs";
import { audioFormat, base64Bytes } from "./dev-audio.mjs";

export function devSpeechConfig(env) {
  const llm = devLlmConfig(env);
  if (llm.protocol !== "mimo")
    throw new Error("development AI speech requires MiMo configuration");
  return {
    url: llm.url,
    key: llm.key,
    asr: "mimo-v2.5-asr",
    tts: "mimo-v2.5-tts",
    voice: env.DEV_TTS_VOICE ?? "mimo_default",
  };
}

export function devEmbeddingConfig(env) {
  let url;
  try {
    url = new URL(env.DEV_OLLAMA_URL);
  } catch {
    throw new Error("development AI requires DEV_OLLAMA_URL");
  }
  if (
    url.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
    url.pathname !== "/api/embed" ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  )
    throw new Error(
      "development AI requires a loopback Ollama /api/embed endpoint"
    );
  const model = env.DEV_EMBEDDING_MODEL;
  if (!["bge-m3", "bge-m3:latest"].includes(model))
    throw new Error(
      "development AI requires the 1024-dimensional bge-m3 embedding contract"
    );
  return { url: url.href, model };
}

async function postJson(url, body, { key, signal, fetcher = fetch } = {}) {
  let response;
  try {
    response = await fetcher(url, {
      method: "POST",
      redirect: "manual",
      signal: signal
        ? AbortSignal.any([signal, AbortSignal.timeout(180000)])
        : AbortSignal.timeout(180000),
      headers: {
        "content-type": "application/json",
        ...(key ? { authorization: `Bearer ${key}` } : {}),
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error("development AI transport failed");
  }
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error(`development AI HTTP ${response.status}`);
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error("development AI empty response");
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 16 * 1024 * 1024)
        throw new Error("development AI response limit");
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.length;
    }
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new Error("development AI invalid or oversized response");
  } finally {
    await reader.cancel().catch(() => {});
  }
}

function speechChoice(data, model) {
  const choice = data?.choices?.[0];
  if (
    data?.model !== model ||
    choice?.finish_reason !== "stop" ||
    !choice.message
  )
    throw new Error("development AI incomplete speech response");
  return choice.message;
}

export async function runDevAsr(env, input, options = {}) {
  const config = devSpeechConfig(env),
    bytes = base64Bytes(input.audio);
  const format = audioFormat(bytes);
  const language = (input.language ?? "auto").toLowerCase().split(/[-_]/)[0];
  if (!["auto", "multi", "zh", "en"].includes(language))
    throw new Error("development AI unsupported ASR language");
  const data = await postJson(
    config.url,
    {
      model: config.asr,
      messages: [
        {
          role: "user",
          content: [
            {
              type: "input_audio",
              input_audio: {
                data: `data:${
                  format === "wav" ? "audio/wav" : "audio/mpeg"
                };base64,${input.audio}`,
              },
            },
          ],
        },
      ],
      asr_options: { language: language === "multi" ? "auto" : language },
      stream: false,
    },
    { ...options, key: config.key }
  );
  const message = speechChoice(data, config.asr),
    seconds = data.usage?.seconds;
  if (
    typeof message.content !== "string" ||
    !Number.isFinite(seconds) ||
    seconds < 0
  )
    throw new Error("development AI invalid ASR transcript");
  const text = message.content.trim();
  return {
    text,
    segments: text
      ? [{ text, start: 0, end: seconds, speaker: "SPEAKER_00" }]
      : [],
    transcription_info: { duration: seconds },
    usage: data.usage,
  };
}

export async function runDevTts(env, input, options = {}) {
  const config = devSpeechConfig(env);
  if (
    typeof input.text !== "string" ||
    !input.text.trim() ||
    input.text.length > 5000
  )
    throw new Error("development AI invalid TTS text");
  if (
    (input.encoding && input.encoding !== "mp3") ||
    (input.output_format && !input.output_format.startsWith("mp3_"))
  )
    throw new Error("development AI TTS adapter supports MP3 output only");
  const data = await postJson(
    config.url,
    {
      model: config.tts,
      messages: [{ role: "assistant", content: input.text }],
      audio: { format: "mp3", voice: config.voice },
      stream: false,
    },
    { ...options, key: config.key }
  );
  const message = speechChoice(data, config.tts),
    encoded = message.audio?.data;
  const bytes = base64Bytes(encoded);
  if (audioFormat(bytes) !== "mp3")
    throw new Error("development AI TTS returned the wrong format");
  return { bytes, encoded, usage: data.usage };
}

export async function runDevEmbedding(env, input, options = {}) {
  const config = devEmbeddingConfig(env),
    texts = Array.isArray(input.text) ? input.text : [input.text];
  if (
    !texts.length ||
    texts.length > 128 ||
    texts.some((text) => typeof text !== "string" || !text.trim()) ||
    texts.reduce((n, text) => n + text.length, 0) > 256000
  )
    throw new Error("development AI invalid embedding input");
  const result = await postJson(
    config.url,
    { model: config.model, input: texts, truncate: false },
    options
  );
  const vectors = result.embeddings;
  if (
    result.model?.replace(/:latest$/, "") !==
      config.model.replace(/:latest$/, "") ||
    !Array.isArray(vectors) ||
    vectors.length !== texts.length ||
    vectors.some(
      (vector) =>
        !Array.isArray(vector) ||
        vector.length !== 1024 ||
        vector.some((n) => !Number.isFinite(n)) ||
        !vector.some((n) => n !== 0)
    )
  )
    throw new Error("development AI invalid Ollama embedding result");
  return { shape: [vectors.length, 1024], data: vectors };
}
