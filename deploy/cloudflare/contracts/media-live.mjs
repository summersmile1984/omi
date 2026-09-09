import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { WebSocket } from "ws";

// Called by the existing credentialed product lane. Every provider request goes
// through the public authenticated application routes; credentials stay in the
// provider's local binding and are not passed to this consumer.
export async function runLiveMedia({ metadata, token, fetcher, pass }) {
  async function request(path, body, contentType = "application/json") {
    const response = await fetcher(metadata.api_origin + path, {
      method: "POST",
      redirect: "error",
      signal: AbortSignal.timeout(180000),
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": contentType,
        origin: metadata.auth_origin,
      },
      body: contentType === "application/json" ? JSON.stringify(body) : body,
    });
    assert.equal(response.status, 200, `POST ${path}`);
    return response;
  }
  const text = "今天下午三点整理工作笔记，然后给团队发送进展更新。";
  const tts = await request("/v1/tts/synthesize", { text, voice_id: "alloy" });
  assert(tts.headers.get("content-type").startsWith("audio/mpeg"));
  const mp3 = Buffer.from(await tts.arrayBuffer());
  const audioFile = resolve(metadata.trace_dir, "live-mimo-tts.mp3");
  writeFileSync(audioFile, mp3, { mode: 0o600 });
  const probe = JSON.parse(
    execFileSync(
      "ffprobe",
      [
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name",
        "-of",
        "json",
        audioFile,
      ],
      { encoding: "utf8", timeout: 10000 }
    )
  );
  assert(
    probe.format.format_name.includes("mp3") &&
      Number(probe.format.duration) > 0
  );
  pass("real-mimo-tts-desktop-mp3");
  const mobile = await request("/v2/tts/synthesize", {
    text: "你好，开始整理笔记。",
  });
  const mobileFile = resolve(metadata.trace_dir, "live-mimo-mobile-tts.mp3");
  writeFileSync(mobileFile, Buffer.from(await mobile.arrayBuffer()), {
    mode: 0o600,
  });
  execFileSync("ffmpeg", ["-v", "error", "-i", mobileFile, "-f", "null", "-"], {
    timeout: 10000,
  });
  pass("real-mimo-tts-mobile-envelope");

  const transcript = await (
    await request("/v1/stt/transcribe-workers-ai", mp3, "audio/mpeg")
  ).json();
  assert(
    typeof transcript.text === "string" && /笔记|工作/.test(transcript.text)
  );
  assert(transcript.segments.length > 0);
  pass("real-mimo-asr-uploaded-audio");

  const embedded = await (
    await request("/v1/embeddings", {
      input: ["今天整理工作笔记", "整理今日工作记录", "海底的珊瑚礁"],
    })
  ).json();
  const vectors = embedded.data.map((row) => row.embedding);
  assert.equal(vectors.length, 3);
  assert(
    vectors.every(
      (vector) => vector.length === 1024 && vector.every(Number.isFinite)
    )
  );
  const cosine = (a, b) =>
    a.reduce((s, n, i) => s + n * b[i], 0) /
    Math.sqrt(
      a.reduce((s, n) => s + n * n, 0) * b.reduce((s, n) => s + n * n, 0)
    );
  assert(cosine(vectors[0], vectors[1]) > cosine(vectors[0], vectors[2]));
  pass("real-ollama-bge-m3-semantic-vectors");

  const pcm = execFileSync(
    "ffmpeg",
    [
      "-v",
      "error",
      "-i",
      audioFile,
      "-f",
      "s16le",
      "-ar",
      "16000",
      "-ac",
      "1",
      "pipe:1",
    ],
    { timeout: 10000, maxBuffer: 2000000 }
  );
  const duration = pcm.length / 32000,
    id = randomUUID();
  const ws = new WebSocket(
    metadata.api_origin.replace("http:", "ws:") +
      `/v4/listen?codec=pcm16&sample_rate=16000&language=zh&client_conversation_id=${id}`,
    { headers: { authorization: `Bearer ${token}` } }
  );
  const frames = [],
    listeners = new Set();
  let closed = false;
  const notify = () => {
    for (const fn of listeners) fn();
  };
  ws.on("message", (bytes) => {
    frames.push(JSON.parse(bytes.toString()));
    notify();
  });
  ws.on("close", () => {
    closed = true;
    notify();
  });
  ws.on("error", () => {
    closed = true;
    notify();
  });
  const until = (predicate) =>
    new Promise((resolve, reject) => {
      const finish = (error) => {
        clearTimeout(timer);
        listeners.delete(check);
        error ? reject(error) : resolve();
      };
      const check = () =>
        predicate()
          ? finish()
          : closed
          ? finish(new Error("live ASR socket closed early"))
          : undefined;
      const timer = setTimeout(
        () => finish(new Error("live ASR deadline exceeded")),
        120000
      );
      listeners.add(check);
      check();
    });
  try {
    await until(() =>
      frames.some((frame) => frame.type === "conversation_session")
    );
    for (let offset = 0; offset < pcm.length; offset += 32000)
      ws.send(pcm.subarray(offset, offset + 32000));
    await until(() =>
      frames
        .flatMap((frame) => (Array.isArray(frame) ? frame : []))
        .some((row) => row.text && row.end >= duration - 0.05)
    );
    const response = await fetcher(
      metadata.api_origin + `/v1/conversations/${id}`,
      {
        headers: { authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(10000),
      }
    );
    assert.equal(response.status, 200);
    const conversation = await response.json();
    assert(
      conversation.transcript_segments?.some((row) =>
        /笔记|工作/.test(row.text)
      )
    );
    pass("real-mimo-websocket-asr-persisted-transcript");
  } finally {
    if (ws.readyState < WebSocket.CLOSING) ws.close();
    if (!closed)
      await new Promise((resolve) => {
        const timer = setTimeout(() => {
          ws.terminate();
          resolve();
        }, 3000);
        ws.once("close", () => {
          clearTimeout(timer);
          resolve();
        });
      });
  }
}
