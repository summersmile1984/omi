import { bytesBase64, pcm16Wav } from "./dev-audio.mjs";
import { runDevAsr } from "./dev-media.mjs";

// Adapt the existing external-ASR segment protocol to bounded HTTP recognition.
// No application state lives here: Realtime owns admission, transcript writes
// and delivery. At most one model request and 20 seconds of queued PCM/session.
export function attachDevAsr(
  socket,
  request,
  env,
  { infer = runDevAsr, idleMs = 350 } = {}
) {
  const url = new URL(request.url, "http://localhost");
  const rate = Number(url.searchParams.get("sample_rate") ?? 8000);
  const channels = Number(url.searchParams.get("channels") ?? 1);
  const codec = url.searchParams.get("codec") ?? "pcm8";
  const language = url.searchParams.get("language") ?? "auto";
  if (
    !Number.isInteger(rate) ||
    rate < 8000 ||
    rate > 48000 ||
    channels !== 1 ||
    !["pcm16", "linear16", "pcm8"].includes(codec)
  ) {
    socket.close(1003, "local ASR requires mono PCM audio");
    return;
  }
  const abort = new AbortController(),
    windowBytes = rate * 2 * 5;
  let pcm = Buffer.alloc(0),
    offset = 0,
    running = false,
    timer,
    closed = false;
  const dispose = () => {
    closed = true;
    clearTimeout(timer);
    abort.abort();
    pcm = Buffer.alloc(0);
  };
  socket.once("close", dispose);
  socket.once("error", dispose);
  async function flush(force) {
    if (
      closed ||
      running ||
      !pcm.length ||
      (!force && pcm.length < windowBytes)
    )
      return;
    running = true;
    try {
      do {
        const count = Math.min(pcm.length, windowBytes),
          chunk = pcm.subarray(0, count);
        pcm = pcm.subarray(count);
        const start = offset,
          duration = chunk.length / (rate * 2);
        offset += duration;
        const result = await infer(
          env,
          { audio: bytesBase64(pcm16Wav(chunk, rate)), language },
          { signal: abort.signal }
        );
        if (closed) return;
        if (result.text)
          socket.send(
            JSON.stringify([
              {
                text: result.text,
                start,
                end: start + duration,
                speaker: "SPEAKER_00",
                is_user: false,
                person_id: null,
              },
            ])
          );
      } while (pcm.length && (force || pcm.length >= windowBytes));
    } catch {
      if (!closed) socket.close(1011, "local MiMo ASR failed");
      dispose();
    } finally {
      running = false;
      if (!closed && pcm.length) {
        clearTimeout(timer);
        timer = setTimeout(() => void flush(true), idleMs);
      }
    }
  }
  socket.on("message", (data, binary) => {
    if (closed) return;
    if (!binary) {
      // Existing providers accept keepalives; flush only explicit finalization.
      try {
        if (
          ["Finalize", "CloseStream"].includes(JSON.parse(data.toString()).type)
        )
          void flush(true);
      } catch {}
      return;
    }
    let bytes = Buffer.from(data);
    if (codec === "pcm8") {
      const linear = Buffer.alloc(bytes.length * 2);
      for (let i = 0; i < bytes.length; i++)
        linear.writeInt16LE((bytes[i] - 128) * 256, i * 2);
      bytes = linear;
    }
    if (bytes.length % 2 || pcm.length + bytes.length > rate * 2 * 20) {
      socket.close(1009, "local ASR audio buffer exceeded");
      dispose();
      return;
    }
    pcm = Buffer.concat([pcm, bytes]);
    clearTimeout(timer);
    void flush(false);
    timer = setTimeout(() => void flush(true), idleMs);
  });
}
