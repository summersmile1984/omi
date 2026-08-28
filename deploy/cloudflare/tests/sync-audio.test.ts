import { describe, expect, it } from "vitest";
import {
  decodeWalToWavChunks,
  parseSyncFilename,
  walFrameViews,
} from "../workers/jobs/sync-audio";

function wal(frames: Uint8Array[]): ArrayBuffer {
  const size = frames.reduce((total, frame) => total + 4 + frame.byteLength, 0);
  const output = new Uint8Array(size);
  const view = new DataView(output.buffer);
  let offset = 0;
  for (const frame of frames) {
    view.setUint32(offset, frame.byteLength, true);
    offset += 4;
    output.set(frame, offset);
    offset += frame.byteLength;
  }
  return output.buffer;
}

describe("sync WAL audio", () => {
  it("parses current Opus and PCM sync filenames", () => {
    expect(
      parseSyncFilename(
        "audio_omibatch_opus_fs320_16000_1_fs320_1787932800.bin",
      ),
    ).toMatchObject({
      captureAt: 1_787_932_800,
      codec: "opus",
      sampleRate: 16_000,
      channels: 1,
      frameSize: 320,
    });
    expect(
      parseSyncFilename("audio_omibatch_pcm16_16000_1_fs160_1787932800000.bin"),
    ).toMatchObject({
      captureAt: 1_787_932_800,
      codec: "pcm16",
      sampleRate: 16_000,
      channels: 1,
      frameSize: 160,
    });
    expect(parseSyncFilename("../audio_1787932800.bin")).toBeNull();
  });

  it("rejects truncated frames instead of accepting partial audio", () => {
    expect(() => walFrameViews(new Uint8Array([4, 0, 0]))).toThrow(
      "truncated frame header",
    );
    expect(() => walFrameViews(new Uint8Array([4, 0, 0, 0, 1, 2]))).toThrow(
      "truncated frame",
    );
  });

  it("decodes PCM16 into bounded WAV chunks", async () => {
    const identity = parseSyncFilename(
      "audio_omibatch_pcm16_16000_1_fs160_1787932800.bin",
    );
    expect(identity).not.toBeNull();
    const first = new Int16Array(160);
    const second = new Int16Array(160);
    first.fill(1_000);
    second.fill(-1_000);
    const chunks = [];
    for await (const chunk of decodeWalToWavChunks(
      wal([new Uint8Array(first.buffer), new Uint8Array(second.buffer)]),
      identity!,
      0.01,
    )) {
      chunks.push(chunk);
    }
    expect(chunks).toHaveLength(2);
    expect(chunks.map((chunk) => chunk.durationSeconds)).toEqual([0.01, 0.01]);
    expect(new TextDecoder().decode(chunks[0].wav.slice(0, 4))).toBe("RIFF");
    expect(new DataView(chunks[0].wav).getUint32(40, true)).toBe(320);
  });

  it("normalizes high-rate stereo PCM to the Worker playback format", async () => {
    const identity = parseSyncFilename(
      "audio_omibatch_pcm16_48000_2_fs480_1787932800.bin",
    );
    expect(identity).not.toBeNull();
    const stereo = new Int16Array(960);
    for (let frame = 0; frame < 480; frame += 1) {
      stereo[frame * 2] = 2_000;
      stereo[frame * 2 + 1] = -1_000;
    }
    const chunks = [];
    for await (const chunk of decodeWalToWavChunks(
      wal([new Uint8Array(stereo.buffer)]),
      identity!,
    )) {
      chunks.push(chunk);
    }

    expect(chunks).toHaveLength(1);
    const view = new DataView(chunks[0].wav);
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(24, true)).toBe(16_000);
    expect(view.getUint32(40, true)).toBe(320);
    expect(view.getInt16(44, true)).toBe(500);
    expect(chunks[0].durationSeconds).toBe(0.01);
  });
});
