import { mkdirSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { PNG } from 'pngjs'

// Deterministic synthetic pixels for engineering fixtures, never a real brand.
export function writeFixtureAssets(root, variant = 'harbor') {
  mkdirSync(join(root, 'assets'), { recursive: true })
  for (const role of ['icon_master', 'logo_light', 'logo_dark']) {
    const size = role === 'icon_master' ? 1024 : 128
    const png = new PNG({ width: size, height: size })
    for (let y = 0; y < size; y++)
      for (let x = 0; x < size; x++) {
        const u = x / size,
          v = y / size,
          i = (y * size + x) * 4
        const upright = (u > 0.23 && u < 0.37) || (u > 0.63 && u < 0.77)
        const bar = variant === 'harbor' ? v > 0.43 && v < 0.57 : Math.abs(v - u) < 0.09
        const mark = v > 0.2 && v < 0.8 && (upright || (u > 0.23 && u < 0.77 && bar))
        const circle = role === 'icon_master' && Math.hypot(u - 0.5, v - 0.5) < 0.47
        const color = role === 'logo_light' || !mark ? 255 : 0
        if (mark || circle) png.data.set([color, color, color, 255], i)
      }
    writeFileSync(join(root, 'assets', `${variant}-${role}.png`), PNG.sync.write(png))
  }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url))
  writeFixtureAssets(process.argv[2], process.argv[3])
