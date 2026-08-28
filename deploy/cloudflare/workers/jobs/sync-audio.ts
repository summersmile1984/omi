import { OpusDecoder, type OpusDecoderSampleRate } from "opus-decoder";

const MAX_WAL_FRAME_BYTES = 65_536;
const MIN_CAPTURE_AT_SECONDS = Date.UTC(2024, 0, 1) / 1000;

export type SyncAudioCodec = "opus" | "pcm16" | "pcm8";

export type SyncFileIdentity = {
  filename: string;
  captureAt: number;
  codec: SyncAudioCodec;
  sampleRate: OpusDecoderSampleRate;
  channels: 1 | 2;
  frameSize: number;
};

export type DecodedWavChunk = {
  wav: ArrayBuffer;
  startSeconds: number;
  durationSeconds: number;
};

function supportedSampleRate(value: number): value is OpusDecoderSampleRate {
  return [8_000, 12_000, 16_000, 24_000, 48_000].includes(value);
}

export function parseSyncFilename(filename: string): SyncFileIdentity | null {
  if (
    !filename ||
    filename.length > 255 ||
    filename.includes("/") ||
    filename.includes("\\") ||
    !filename.endsWith(".bin")
  ) {
    return null;
  }
  const timestampText = filename.slice(0, -4).split("_").at(-1) || "";
  const rawTimestamp = Number(timestampText);
  if (!Number.isFinite(rawTimestamp) || rawTimestamp <= 0) return null;
  const captureAt =
    rawTimestamp > 10_000_000_000 ? rawTimestamp / 1000 : rawTimestamp;
  if (!Number.isFinite(captureAt) || captureAt < MIN_CAPTURE_AT_SECONDS)
    return null;

  const codec: SyncAudioCodec = filename.includes("_pcm16_")
    ? "pcm16"
    : filename.includes("_pcm8_")
      ? "pcm8"
      : "opus";
  const rateMatch = filename.match(
    /_(8000|12000|16000|24000|48000)_[12]_fs\d+_/,
  );
  const pcmRateMatch = filename.match(/_pcm(?:8|16)_(\d+)_/);
  const candidateRate = Number(pcmRateMatch?.[1] || rateMatch?.[1] || 16_000);
  const sampleRate = supportedSampleRate(candidateRate)
    ? candidateRate
    : 16_000;
  const channelsMatch = filename.match(
    /_(?:8000|12000|16000|24000|48000)_([12])_fs\d+_/,
  );
  const channels = channelsMatch?.[1] === "2" ? 2 : 1;
  const frameSizeValue = Number(filename.match(/_fs(\d+)/)?.[1] || 160);
  const frameSize =
    Number.isInteger(frameSizeValue) &&
    frameSizeValue > 0 &&
    frameSizeValue <= 5_760
      ? frameSizeValue
      : 160;
  return {
    filename,
    captureAt: Math.floor(captureAt),
    codec,
    // The legacy Opus path always normalizes to 16 kHz. PCM preserves the
    // filename-declared rate because its bytes are already raw samples.
    sampleRate: codec === "opus" ? 16_000 : sampleRate,
    channels,
    frameSize,
  };
}

export function walFrameViews(bytes: Uint8Array): Uint8Array[] {
  const frames: Uint8Array[] = [];
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let offset = 0;
  while (offset < bytes.byteLength) {
    if (bytes.byteLength - offset < 4)
      throw new Error("sync audio has a truncated frame header");
    const length = view.getUint32(offset, true);
    offset += 4;
    if (!length || length > MAX_WAL_FRAME_BYTES)
      throw new Error("sync audio has an invalid frame length");
    if (offset + length > bytes.byteLength)
      throw new Error("sync audio has a truncated frame");
    frames.push(bytes.subarray(offset, offset + length));
    offset += length;
  }
  return frames;
}

function floatToPcm16(value: number): number {
  const bounded = Math.max(-1, Math.min(1, value));
  return bounded < 0
    ? Math.round(bounded * 32_768)
    : Math.round(bounded * 32_767);
}

function pcm16FromFrame(
  frame: Uint8Array,
  codec: SyncAudioCodec,
): Int16Array | null {
  if (codec === "pcm8") {
    const samples = new Int16Array(frame.byteLength);
    for (let index = 0; index < frame.byteLength; index += 1) {
      samples[index] = (frame[index] - 128) << 8;
    }
    return samples;
  }
  if (frame.byteLength % 2 !== 0) return null;
  const samples = new Int16Array(frame.byteLength / 2);
  const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = view.getInt16(index * 2, true);
  }
  return samples;
}

export function pcm16ToWav(
  samples: Int16Array,
  sampleRate: number,
  channels: number,
): ArrayBuffer {
  const dataBytes = samples.byteLength;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);
  const ascii = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index += 1)
      view.setUint8(offset + index, value.charCodeAt(index));
  };
  ascii(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * channels * 2, true);
  view.setUint16(32, channels * 2, true);
  view.setUint16(34, 16, true);
  ascii(36, "data");
  view.setUint32(40, dataBytes, true);
  new Int16Array(buffer, 44).set(samples);
  return buffer;
}

/**
 * Decode one WAL file into bounded WAV windows. The decoder state survives
 * window boundaries, while only one PCM window is resident at a time.
 */
export async function* decodeWalToWavChunks(
  raw: ArrayBuffer,
  identity: SyncFileIdentity,
  maxChunkSeconds = 120,
): AsyncGenerator<DecodedWavChunk, void, unknown> {
  const frames = walFrameViews(new Uint8Array(raw));
  if (!frames.length) throw new Error("sync audio has no readable frames");

  const maxSamples = Math.max(
    identity.frameSize,
    Math.floor(identity.sampleRate * identity.channels * maxChunkSeconds),
  );
  let chunk = new Int16Array(maxSamples);
  let chunkSamples = 0;
  let totalSamples = 0;
  let decoder: OpusDecoder<OpusDecoderSampleRate> | null = null;

  const flush = (): DecodedWavChunk | null => {
    if (!chunkSamples) return null;
    const complete = chunk.slice(0, chunkSamples);
    const startSeconds =
      totalSamples / (identity.sampleRate * identity.channels);
    totalSamples += chunkSamples;
    const durationSeconds =
      chunkSamples / (identity.sampleRate * identity.channels);
    chunk = new Int16Array(maxSamples);
    chunkSamples = 0;
    return {
      wav: pcm16ToWav(complete, identity.sampleRate, identity.channels),
      startSeconds,
      durationSeconds,
    };
  };

  try {
    if (identity.codec === "opus") {
      decoder = new OpusDecoder({
        sampleRate: identity.sampleRate,
        channels: identity.channels,
        streamCount: 1,
        coupledStreamCount: identity.channels === 2 ? 1 : 0,
        channelMappingTable: identity.channels === 2 ? [0, 1] : [0],
      });
      await decoder.ready;
    }

    for (const frame of frames) {
      let decoded: Int16Array | null;
      if (decoder) {
        try {
          const result = decoder.decodeFrame(frame);
          if (
            result.errors.length ||
            !result.samplesDecoded ||
            !result.channelData.length
          )
            break;
          const interleaved = new Int16Array(
            result.samplesDecoded * identity.channels,
          );
          for (let sample = 0; sample < result.samplesDecoded; sample += 1) {
            for (let channel = 0; channel < identity.channels; channel += 1) {
              const source =
                result.channelData[
                  Math.min(channel, result.channelData.length - 1)
                ];
              interleaved[sample * identity.channels + channel] = floatToPcm16(
                source[sample] || 0,
              );
            }
          }
          decoded = interleaved;
        } catch {
          break;
        }
      } else {
        decoded = pcm16FromFrame(frame, identity.codec);
        if (!decoded) break;
      }

      let offset = 0;
      while (offset < decoded.length) {
        const copied = Math.min(
          decoded.length - offset,
          chunk.length - chunkSamples,
        );
        chunk.set(decoded.subarray(offset, offset + copied), chunkSamples);
        chunkSamples += copied;
        offset += copied;
        if (chunkSamples === chunk.length) {
          const ready = flush();
          if (ready) yield ready;
        }
      }
    }
    const ready = flush();
    if (ready) yield ready;
    if (!totalSamples) throw new Error("sync audio could not be decoded");
  } finally {
    decoder?.free();
  }
}
