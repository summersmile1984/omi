import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { resolve, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'
import { PNG, readAsset, resize } from '../../../scripts/brand/raster/png.mjs'
import { packIco } from '../scripts/lib/icon-raster.mjs'

export const ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex')

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
