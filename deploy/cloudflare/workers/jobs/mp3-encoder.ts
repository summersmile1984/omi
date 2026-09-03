import createMp3Encoder from "@audio/encode-mp3";

const ENCODER_CHUNK_SAMPLES = 1152 * 1024;
const MAX_PCM_BYTES = 64 * 1024 * 1024;

/**
 * Encode the canonical playback PCM representation to the legacy wire format.
 *
 * The legacy service uses pydub/ffmpeg to emit a mono 16 kHz CBR MP3 at 48
 * kbps.  Workers cannot load the host ffmpeg binary, so this boundary uses
 * the pinned libmp3lame WASM encoder instead.  The bytes are intentionally
 * generated from the same canonical PCM (rather than concatenating MP3
 * streams), which keeps the conversation artifact one valid MP3 stream.
 */
export async function encodePcm16MonoToMp3(
  pcm: Uint8Array,
  sampleRate = 16_000,
  bitrate = 48,
): Promise<Uint8Array> {
  if (
    !Number.isInteger(sampleRate) ||
    sampleRate < 8_000 ||
    sampleRate > 48_000 ||
    !Number.isInteger(bitrate) ||
    bitrate <= 0 ||
    pcm.byteLength === 0 ||
    pcm.byteLength % 2 !== 0 ||
    pcm.byteLength > MAX_PCM_BYTES
  ) {
    throw new Error("MP3 encoder input is invalid");
  }

  const encoder = await createMp3Encoder({
    sampleRate,
    channels: 1,
    bitrate,
  });
  const chunks: Uint8Array[] = [];
  try {
    const samples = new Int16Array(
      pcm.buffer,
      pcm.byteOffset,
      pcm.byteLength / Int16Array.BYTES_PER_ELEMENT,
    );
    for (let offset = 0; offset < samples.length; offset += ENCODER_CHUNK_SAMPLES) {
      const end = Math.min(offset + ENCODER_CHUNK_SAMPLES, samples.length);
      const floats = new Float32Array(end - offset);
      for (let index = offset; index < end; index += 1)
        floats[index - offset] = samples[index] / 32_768;
      const encoded = encoder.encode([floats]);
      if (encoded.byteLength) chunks.push(encoded);
    }
    const tail = encoder.flush();
    if (tail.byteLength) chunks.push(tail);
  } finally {
    encoder.free();
  }

  const total = chunks.reduce((size, chunk) => size + chunk.byteLength, 0);
  if (!total) throw new Error("MP3 encoder produced no output");
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}
