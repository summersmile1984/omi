// Audio wire helpers shared by the local Worker adapter and its PCM ASR bridge.
export function base64Bytes(value, limit = 10 * 1024 * 1024) {
  if (
    typeof value !== "string" ||
    !value ||
    value.length > Math.ceil(limit / 3) * 4 ||
    value.length % 4 ||
    !/^[A-Za-z0-9+/]*={0,2}$/.test(value)
  )
    throw new Error("development AI invalid base64 audio");
  return Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
}

export function bytesBase64(value) {
  let text = "";
  for (let i = 0; i < value.length; i += 8192)
    text += String.fromCharCode(...value.subarray(i, i + 8192));
  return btoa(text);
}

export function audioFormat(bytes) {
  const tag = (start, length) =>
    String.fromCharCode(...bytes.subarray(start, start + length));
  if (bytes.length >= 44 && tag(0, 4) === "RIFF" && tag(8, 4) === "WAVE")
    return "wav";
  if (
    bytes.length > 4 &&
    (tag(0, 3) === "ID3" || (bytes[0] === 255 && (bytes[1] & 0xe0) === 0xe0))
  )
    return "mp3";
  throw new Error("development AI supports WAV or MP3 audio only");
}

export function pcm16Wav(pcm, sampleRate) {
  if (
    !Number.isInteger(sampleRate) ||
    sampleRate < 8000 ||
    sampleRate > 48000 ||
    !pcm.length ||
    pcm.length % 2
  )
    throw new Error("development AI invalid PCM16 audio");
  const bytes = new Uint8Array(44 + pcm.length),
    view = new DataView(bytes.buffer);
  for (const [offset, text] of [
    [0, "RIFF"],
    [8, "WAVE"],
    [12, "fmt "],
    [36, "data"],
  ])
    bytes.set(new TextEncoder().encode(text), offset);
  view.setUint32(4, bytes.length - 8, true);
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  view.setUint32(40, pcm.length, true);
  bytes.set(pcm, 44);
  return bytes;
}
