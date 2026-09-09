import { EventEmitter } from "node:events";
import { afterEach, expect, it, vi } from "vitest";
import {
  runDevAsr,
  runDevTts,
  runDevEmbedding,
} from "../contracts/dev-media.mjs";
import { pcm16Wav, bytesBase64, base64Bytes } from "../contracts/dev-audio.mjs";
import { attachDevAsr } from "../contracts/dev-asr-socket.mjs";
const env = {
  DEV_LLM_URL: "https://mimo.example.test/v1/chat/completions",
  DEV_LLM_MODEL: "mimo-v2.5",
  DEV_LLM_PROTOCOL: "mimo",
  DEV_LLM_API_KEY: "secret",
  DEV_OLLAMA_URL: "http://127.0.0.1:11434/api/embed",
  DEV_EMBEDDING_MODEL: "bge-m3",
};
const wav = pcm16Wav(new Uint8Array(32000), 16000);
const reply = (model, message, seconds = 1) => ({
  model,
  choices: [{ finish_reason: "stop", message }],
  usage: { seconds },
});
afterEach(() => vi.useRealTimers());

it("maps WAV to the documented MiMo ASR request and preserves transcript/duration", async () => {
  const fetcher = vi.fn(async () =>
    Response.json(reply("mimo-v2.5-asr", { content: "整理笔记" }))
  );
  const result = await runDevAsr(
    env,
    { audio: bytesBase64(wav), language: "zh-CN" },
    { fetcher }
  );
  const body = JSON.parse(fetcher.mock.calls[0][1].body);
  expect(body).toMatchObject({
    model: "mimo-v2.5-asr",
    asr_options: { language: "zh" },
    stream: false,
  });
  expect(body.messages[0].content[0].input_audio.data).toBe(
    "data:audio/wav;base64," + bytesBase64(wav)
  );
  expect(result).toMatchObject({
    text: "整理笔记",
    segments: [{ start: 0, end: 1, speaker: "SPEAKER_00" }],
  });
});

it("sends the exact TTS text and returns MP3 bytes without prompt augmentation", async () => {
  const mp3 = new Uint8Array([73, 68, 51, 4, 0, 0, 0, 0, 0, 0]);
  const fetcher = vi.fn(async () =>
    Response.json(reply("mimo-v2.5-tts", { audio: { data: bytesBase64(mp3) } }))
  );
  const result = await runDevTts(
    env,
    { text: "你好。", encoding: "mp3", speaker: "angus" },
    { fetcher }
  );
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({
    model: "mimo-v2.5-tts",
    messages: [{ role: "assistant", content: "你好。" }],
    audio: { format: "mp3", voice: "mimo_default" },
    stream: false,
  });
  expect(result.bytes).toEqual(mp3);
});

it("uses Ollama batch embeddings without sending the MiMo key or truncating text", async () => {
  const vector = Array(1024).fill(0.125);
  const fetcher = vi.fn(async () =>
    Response.json({ model: "bge-m3", embeddings: [vector, vector] })
  );
  const result = await runDevEmbedding(
    env,
    { text: ["笔记", "tasks"] },
    { fetcher }
  );
  const [url, init] = fetcher.mock.calls[0];
  expect(url).toBe(env.DEV_OLLAMA_URL);
  expect(init.headers).not.toHaveProperty("authorization");
  expect(JSON.parse(init.body)).toEqual({
    model: "bge-m3",
    input: ["笔记", "tasks"],
    truncate: false,
  });
  expect(result).toEqual({ shape: [2, 1024], data: [vector, vector] });
});

it.each([
  [],
  [Array(768).fill(0.1)],
  [Array(1024).fill(0)],
  [Array(1024).fill(null)],
])("rejects invalid Ollama vectors", async (vectors) => {
  await expect(
    runDevEmbedding(
      env,
      { text: ["test"] },
      {
        fetcher: async () =>
          Response.json({ model: "bge-m3", embeddings: vectors }),
      }
    )
  ).rejects.toThrow("invalid Ollama");
});

it.each([302, 401, 429, 503])(
  "fails speech HTTP %s without returning synthetic audio or exposing the body",
  async (status) => {
    const fetcher = vi.fn(async () => new Response("secret", { status }));
    await expect(
      runDevAsr(env, { audio: bytesBase64(wav) }, { fetcher })
    ).rejects.toThrow(`development AI HTTP ${status}`);
    expect(fetcher).toHaveBeenCalledOnce();
  }
);

it("rejects incomplete speech, invalid audio and non-MP3 output contracts", async () => {
  await expect(
    runDevAsr(
      env,
      { audio: bytesBase64(wav) },
      {
        fetcher: async () =>
          Response.json(reply("wrong-model", { content: "text" })),
      }
    )
  ).rejects.toThrow("incomplete speech");
  await expect(
    runDevTts(
      env,
      { text: "hello" },
      {
        fetcher: async () =>
          Response.json(
            reply("mimo-v2.5-tts", { audio: { data: bytesBase64(wav) } })
          ),
      }
    )
  ).rejects.toThrow("wrong format");
  await expect(
    runDevTts(env, { text: "hello", output_format: "opus_48000_128" })
  ).rejects.toThrow("MP3 output only");
  expect(() => base64Bytes("not audio!")).toThrow("base64");
  expect(base64Bytes(bytesBase64(new Uint8Array(1024 * 1024)))).toHaveLength(
    1024 * 1024
  );
});

function socket() {
  const value = new EventEmitter();
  value.send = vi.fn();
  value.close = vi.fn(() => value.emit("close"));
  return value;
}

it("decodes unsigned PCM8 using the existing realtime wire convention", async () => {
  vi.useFakeTimers();
  const ws = socket();
  const infer = vi.fn(async () => ({ text: "audio" }));
  attachDevAsr(ws, { url: "/?codec=pcm8&sample_rate=8000" }, env, { infer });
  ws.emit("message", Buffer.from([0, 128, 255]), true);
  await vi.advanceTimersByTimeAsync(350);
  const bytes = base64Bytes(infer.mock.calls[0][1].audio),
    view = new DataView(bytes.buffer);
  expect([
    view.getInt16(44, true),
    view.getInt16(46, true),
    view.getInt16(48, true),
  ]).toEqual([-32768, 0, 32512]);
  ws.emit("close");
});

it("flushes short PCM windows, serializes model calls, and assigns monotonic segment times", async () => {
  vi.useFakeTimers();
  const ws = socket();
  const infer = vi.fn(async () => ({ text: "真实识别" }));
  attachDevAsr(
    ws,
    { url: "/?codec=pcm16&sample_rate=16000&language=zh" },
    env,
    { infer }
  );
  ws.emit("message", Buffer.alloc(16000), true);
  await vi.advanceTimersByTimeAsync(350);
  ws.emit("message", Buffer.alloc(32000), true);
  await vi.advanceTimersByTimeAsync(350);
  expect(ws.send.mock.calls.map(([data]) => JSON.parse(data)[0])).toMatchObject(
    [
      { text: "真实识别", start: 0, end: 0.5, is_user: false },
      { start: 0.5, end: 1.5 },
    ]
  );
  expect(base64Bytes(infer.mock.calls[0][1].audio).length).toBe(16044);
  ws.emit("close");
});

it("aborts on disconnect and never publishes a late transcript", async () => {
  vi.useFakeTimers();
  const ws = socket();
  let finish;
  const infer = vi.fn(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      })
  );
  attachDevAsr(ws, { url: "/?codec=pcm16&sample_rate=16000" }, env, { infer });
  ws.emit("message", Buffer.alloc(32000), true);
  await vi.advanceTimersByTimeAsync(350);
  ws.emit("close");
  finish({ text: "late" });
  await vi.advanceTimersByTimeAsync(1);
  expect(infer.mock.calls[0][2].signal.aborted).toBe(true);
  expect(ws.send).not.toHaveBeenCalled();
});

it("closes on provider failure or excess buffered audio", async () => {
  vi.useFakeTimers();
  const ws = socket();
  attachDevAsr(ws, { url: "/?codec=pcm16&sample_rate=16000" }, env, {
    infer: async () => {
      throw new Error("private upstream body");
    },
  });
  ws.emit("message", Buffer.alloc(32000), true);
  await vi.advanceTimersByTimeAsync(350);
  expect(ws.close).toHaveBeenCalledWith(1011, "local MiMo ASR failed");
  const full = socket();
  attachDevAsr(full, { url: "/?codec=pcm16&sample_rate=16000" }, env);
  full.emit("message", Buffer.alloc(700000), true);
  expect(full.close).toHaveBeenCalledWith(
    1009,
    "local ASR audio buffer exceeded"
  );
});
