import {
  realpathSync,
  openSync,
  closeSync,
  fstatSync,
  readSync,
} from "node:fs";
import { resolve, relative, isAbsolute } from "node:path";
import { createHash } from "node:crypto";
import { PNG } from "pngjs";
export { PNG };
const MAX_BYTES = 16 * 1024 * 1024;
const MAX_EDGE = 2048;
const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");

export function readAsset(root, name, reference) {
  const base = realpathSync(root);
  if (
    typeof reference !== "string" ||
    !reference ||
    isAbsolute(reference) ||
    reference.split(/[\\/]/).includes("..")
  )
    throw new Error(
      `Asset ${name} must be a relative file within the manifest directory`,
    );
  const path = realpathSync(resolve(base, reference));
  if (relative(base, path).startsWith("..") || isAbsolute(relative(base, path)))
    throw new Error(`Asset ${name} escapes the manifest directory`);
  const fd = openSync(path, "r");
  let bytes;
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.size > MAX_BYTES)
      throw new Error(`Asset ${name} exceeds the file budget`);
    bytes = Buffer.alloc(stat.size);
    let count = 0;
    while (count < bytes.length) {
      const read = readSync(fd, bytes, count, bytes.length - count, null);
      if (!read) throw new Error(`Asset ${name} changed while being read`);
      count += read;
    }
    if (readSync(fd, Buffer.alloc(1), 0, 1, null))
      throw new Error(`Asset ${name} changed while being read`);
  } finally {
    closeSync(fd);
  }
  if (
    bytes.length > MAX_BYTES ||
    bytes.length < 33 ||
    !bytes.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex")) ||
    bytes.toString("ascii", 12, 16) !== "IHDR"
  )
    throw new Error(`Asset ${name} must be a bounded static PNG`);
  const width = bytes.readUInt32BE(16),
    height = bytes.readUInt32BE(20);
  const minimum = name === "icon_master" ? 1024 : 32;
  if (
    width < minimum ||
    height < minimum ||
    width > MAX_EDGE ||
    height > MAX_EDGE ||
    bytes[24] !== 8 ||
    (name === "icon_master" && width !== height)
  )
    throw new Error(`Asset ${name} has unsupported dimensions or bit depth`);
  let offset = 8,
    ended = false;
  while (offset + 12 <= bytes.length) {
    const length = bytes.readUInt32BE(offset),
      kind = bytes.toString("ascii", offset + 4, offset + 8);
    if (
      offset + length + 12 > bytes.length ||
      ["acTL", "fcTL", "fdAT"].includes(kind)
    )
      throw new Error(`Asset ${name} has malformed or animated PNG chunks`);
    offset += length + 12;
    if (kind === "IEND") {
      ended = true;
      break;
    }
  }
  if (!ended || offset !== bytes.length)
    throw new Error(`Asset ${name} has a malformed PNG boundary`);
  const png = PNG.sync.read(bytes, { checkCRC: true });
  if (!png.data.some((value, index) => index % 4 === 3 && value > 0))
    throw new Error(`Asset ${name} is fully transparent`);
  return { png, sha256: digest(bytes), bytes: bytes.length, width, height };
}

// Fit without cropping; bilinear interpolation uses premultiplied alpha so
// transparent source pixels cannot introduce a dark fringe around the mark.
export function resize(source, size) {
  const output = new PNG({ width: size, height: size });
  const scale = Math.min(size / source.width, size / source.height);
  const width = source.width * scale,
    height = source.height * scale;
  const left = (size - width) / 2,
    top = (size - height) / 2;
  for (let y = 0; y < size; y++)
    for (let x = 0; x < size; x++) {
      if (
        x + 0.5 < left ||
        x + 0.5 > left + width ||
        y + 0.5 < top ||
        y + 0.5 > top + height
      )
        continue;
      const sx = Math.max(
        0,
        Math.min(source.width - 1, (x + 0.5 - left) / scale - 0.5),
      );
      const sy = Math.max(
        0,
        Math.min(source.height - 1, (y + 0.5 - top) / scale - 0.5),
      );
      const ix = Math.floor(sx),
        iy = Math.floor(sy),
        fx = sx - ix,
        fy = sy - iy;
      const samples = [
        [ix, iy, (1 - fx) * (1 - fy)],
        [Math.min(ix + 1, source.width - 1), iy, fx * (1 - fy)],
        [ix, Math.min(iy + 1, source.height - 1), (1 - fx) * fy],
        [
          Math.min(ix + 1, source.width - 1),
          Math.min(iy + 1, source.height - 1),
          fx * fy,
        ],
      ];
      const channels = [0, 0, 0, 0];
      for (const [px, py, weight] of samples) {
        const i = (py * source.width + px) * 4,
          alpha = source.data[i + 3] / 255;
        channels[3] += alpha * weight;
        for (let c = 0; c < 3; c++)
          channels[c] += source.data[i + c] * alpha * weight;
      }
      const o = (y * size + x) * 4;
      for (let c = 0; c < 3; c++)
        output.data[o + c] = channels[3]
          ? Math.round(channels[c] / channels[3])
          : 0;
      output.data[o + 3] = Math.round(channels[3] * 255);
    }
  return output;
}
