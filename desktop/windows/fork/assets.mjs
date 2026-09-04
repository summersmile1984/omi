import {
  readFileSync,
  writeFileSync,
  mkdirSync,
  realpathSync,
  openSync,
  closeSync,
  fstatSync,
  readSync
} from 'node:fs'
import { resolve, relative, join, isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'
import { PNG } from 'pngjs'
import { packIco } from '../scripts/lib/icon-raster.mjs'

const MAX_BYTES = 16 * 1024 * 1024
const MAX_EDGE = 2048
export const ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex')

export function readAsset(root, name, reference) {
  const base = realpathSync(root)
  if (
    typeof reference !== 'string' ||
    !reference ||
    isAbsolute(reference) ||
    reference.split(/[\\/]/).includes('..')
  )
    throw new Error(`Asset ${name} must be a relative file within the manifest directory`)
  const path = realpathSync(resolve(base, reference))
  if (relative(base, path).startsWith('..') || isAbsolute(relative(base, path)))
    throw new Error(`Asset ${name} escapes the manifest directory`)
  const fd = openSync(path, 'r')
  let bytes
  try {
    const stat = fstatSync(fd)
    if (!stat.isFile() || stat.size > MAX_BYTES)
      throw new Error(`Asset ${name} exceeds the file budget`)
    bytes = Buffer.alloc(stat.size)
    let count = 0
    while (count < bytes.length) {
      const read = readSync(fd, bytes, count, bytes.length - count, null)
      if (!read) throw new Error(`Asset ${name} changed while being read`)
      count += read
    }
    if (readSync(fd, Buffer.alloc(1), 0, 1, null))
      throw new Error(`Asset ${name} changed while being read`)
  } finally {
    closeSync(fd)
  }
  if (
    bytes.length > MAX_BYTES ||
    bytes.length < 33 ||
    !bytes.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex')) ||
    bytes.toString('ascii', 12, 16) !== 'IHDR'
  )
    throw new Error(`Asset ${name} must be a bounded static PNG`)
  const width = bytes.readUInt32BE(16),
    height = bytes.readUInt32BE(20)
  const minimum = name === 'icon_master' ? 1024 : 32
  if (
    width < minimum ||
    height < minimum ||
    width > MAX_EDGE ||
    height > MAX_EDGE ||
    bytes[24] !== 8 ||
    (name === 'icon_master' && width !== height)
  )
    throw new Error(`Asset ${name} has unsupported dimensions or bit depth`)
  let offset = 8,
    ended = false
  while (offset + 12 <= bytes.length) {
    const length = bytes.readUInt32BE(offset),
      kind = bytes.toString('ascii', offset + 4, offset + 8)
    if (offset + length + 12 > bytes.length || ['acTL', 'fcTL', 'fdAT'].includes(kind))
      throw new Error(`Asset ${name} has malformed or animated PNG chunks`)
    offset += length + 12
    if (kind === 'IEND') {
      ended = true
      break
    }
  }
  if (!ended || offset !== bytes.length)
    throw new Error(`Asset ${name} has a malformed PNG boundary`)
  const png = PNG.sync.read(bytes, { checkCRC: true })
  if (!png.data.some((value, index) => index % 4 === 3 && value > 0))
    throw new Error(`Asset ${name} is fully transparent`)
  return { png, sha256: digest(bytes), bytes: bytes.length, width, height }
}

// Fit without cropping; bilinear interpolation uses premultiplied alpha so
// transparent source pixels cannot introduce a dark fringe around the mark.
export function resize(source, size) {
  const output = new PNG({ width: size, height: size })
  const scale = Math.min(size / source.width, size / source.height)
  const width = source.width * scale,
    height = source.height * scale
  const left = (size - width) / 2,
    top = (size - height) / 2
  for (let y = 0; y < size; y++)
    for (let x = 0; x < size; x++) {
      if (x + 0.5 < left || x + 0.5 > left + width || y + 0.5 < top || y + 0.5 > top + height)
        continue
      const sx = Math.max(0, Math.min(source.width - 1, (x + 0.5 - left) / scale - 0.5))
      const sy = Math.max(0, Math.min(source.height - 1, (y + 0.5 - top) / scale - 0.5))
      const ix = Math.floor(sx),
        iy = Math.floor(sy),
        fx = sx - ix,
        fy = sy - iy
      const samples = [
        [ix, iy, (1 - fx) * (1 - fy)],
        [Math.min(ix + 1, source.width - 1), iy, fx * (1 - fy)],
        [ix, Math.min(iy + 1, source.height - 1), (1 - fx) * fy],
        [Math.min(ix + 1, source.width - 1), Math.min(iy + 1, source.height - 1), fx * fy]
      ]
      const channels = [0, 0, 0, 0]
      for (const [px, py, weight] of samples) {
        const i = (py * source.width + px) * 4,
          alpha = source.data[i + 3] / 255
        channels[3] += alpha * weight
        for (let c = 0; c < 3; c++) channels[c] += source.data[i + c] * alpha * weight
      }
      const o = (y * size + x) * 4
      for (let c = 0; c < 3; c++)
        output.data[o + c] = channels[3] ? Math.round(channels[c] / channels[3]) : 0
      output.data[o + 3] = Math.round(channels[3] * 255)
    }
  return output
}

export function trayFrame(source, state, size = 32) {
  const frame = resize(source, size)
  if (state === 'paused')
    for (let i = 3; i < frame.data.length; i += 4) frame.data[i] = Math.round(frame.data[i] * 0.55)
  if (state === 'listening') {
    const center = size * 0.77,
      radius = size * 0.17,
      moat = size * 0.07
    for (let y = 0; y < size; y++)
      for (let x = 0; x < size; x++) {
        const distance = Math.hypot(x + 0.5 - center, y + 0.5 - center),
          i = (y * size + x) * 4
        if (distance < radius + moat) frame.data.fill(0, i, i + 4)
        if (distance < radius) frame.data.set([255, 255, 255, 255], i)
      }
  }
  return frame
}

export function buildAssets(root, refs, stage) {
  // Validate every consumed asset before emitting anything. Splash has no
  // Electron consumer in this package and is explicitly recorded as unconsumed.
  const inputs = Object.fromEntries(
    ['icon_master', 'logo_light', 'logo_dark'].map((name) => [
      name,
      readAsset(root, name, refs[name])
    ])
  )
  const outputs = {}
  function write(path, bytes) {
    const target = join(stage, path)
    mkdirSync(resolve(target, '..'), { recursive: true })
    writeFileSync(target, bytes)
    outputs[path] = { sha256: digest(bytes), bytes: bytes.length }
  }
  for (const name of ['logo_light', 'logo_dark'])
    write(`fork/assets/${name}.png`, PNG.sync.write(inputs[name].png))
  const icon = inputs.icon_master.png
  write('resources/fork/icon-master.png', PNG.sync.write(resize(icon, 1024)))
  write('resources/fork/icon.png', PNG.sync.write(resize(icon, 256)))
  write('resources/fork/icon.ico', packIco(ICON_SIZES.map((size) => [size, resize(icon, size)])))
  for (const state of ['idle', 'listening', 'paused'])
    write(
      `resources/fork/tray/${state}.png`,
      PNG.sync.write(trayFrame(inputs.logo_light.png, state))
    )
  const texture = resize(inputs.logo_light.png, 64)
  for (let i = 0; i < texture.data.length; i += 4)
    for (let c = 0; c < 3; c++)
      texture.data[i + c] = Math.round((texture.data[i + c] * texture.data[i + 3]) / 255)
  write(
    'fork/renderer/brand-texture.generated.ts',
    Buffer.from(
      `export const brandTexture = ${JSON.stringify({ width: 64, height: 64, premultiplied: true, rgba: Buffer.from(texture.data).toString('base64') })} as const\n`
    )
  )
  const record = {
    inputs: Object.fromEntries(
      Object.entries(inputs).map(([name, { png, ...info }]) => [
        name,
        { ...info, reference: refs[name] }
      ])
    ),
    outputs,
    unconsumed: { splash: 'No Electron splash consumer in this package' },
    scope:
      'Local Electron PNG/ICO and renderer texture; no signing, installer or other-platform qualification'
  }
  writeFileSync(join(stage, 'fork/asset-coverage.json'), JSON.stringify(record, null, 2) + '\n')
  return record
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [root, input, stage] = process.argv.slice(2)
  buildAssets(root, JSON.parse(readFileSync(input, 'utf8')), stage)
}
