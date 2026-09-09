import { afterEach, expect, it } from 'vitest'
import { mkdtempSync, rmSync, readFileSync, writeFileSync, symlinkSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PNG, readAsset, resize } from '../../../../scripts/brand/raster/png.mjs'
import { buildAssets, ICON_SIZES } from '../assets.mjs'
import { writeFixtureAssets } from '../../../../scripts/brand/raster/fixture.mjs'
const roots: string[] = []
function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'brand-assets-'))
  roots.push(root)
  writeFixtureAssets(root)
  return root
}
afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true })
})
const refs = Object.fromEntries(
  ['icon_master', 'logo_light', 'logo_dark'].map((name) => [name, `assets/harbor-${name}.png`])
)

it('builds actual alpha-correct PNG/ICO frames and distinct states from the selected assets', () => {
  const root = fixture(),
    stage = join(root, 'stage')
  const result = buildAssets(root, refs, stage)
  const ico = readFileSync(join(stage, 'resources/fork/icon.ico'))
  expect(ico.readUInt16LE(2)).toBe(1)
  expect(ico.readUInt16LE(4)).toBe(ICON_SIZES.length)
  for (let n = 0; n < ICON_SIZES.length; n++) {
    const entry = 6 + n * 16,
      size = ICON_SIZES[n]
    const data = ico.subarray(
      ico.readUInt32LE(entry + 12),
      ico.readUInt32LE(entry + 12) + ico.readUInt32LE(entry + 8)
    )
    const png = PNG.sync.read(data)
    expect([png.width, png.height]).toEqual([size, size])
    expect(png.data[3]).toBe(0)
    expect(png.data.some((value, index) => index % 4 === 3 && value === 255)).toBe(true)
  }
  const states = ['idle', 'listening', 'paused'].map(
    (state) => result.outputs[`resources/fork/tray/${state}.png`].sha256
  )
  expect(new Set(states).size).toBe(3)
  writeFixtureAssets(root, 'field')
  const other = buildAssets(
    root,
    Object.fromEntries(Object.keys(refs).map((name) => [name, `assets/field-${name}.png`])),
    join(root, 'other')
  )
  expect(other.outputs['resources/fork/icon.png'].sha256).not.toBe(
    result.outputs['resources/fork/icon.png'].sha256
  )
  const sample = new PNG({ width: 2, height: 2 })
  sample.data.set([255, 255, 255, 255, 0, 0, 0, 0, 255, 255, 255, 255, 0, 0, 0, 0])
  expect([...resize(sample, 1).data]).toEqual([255, 255, 255, 128])
})

it('fails before output on missing, escape, corrupt, oversized and invisible assets', () => {
  const root = fixture(),
    stage = join(root, 'stage')
  expect(() => buildAssets(root, { ...refs, logo_dark: 'missing.png' }, stage)).toThrow()
  expect(existsSync(stage)).toBe(false)
  expect(() => readAsset(root, 'logo_light', '../elsewhere.png')).toThrow(/relative file/)
  const outside = fixture()
  symlinkSync(join(outside, refs.logo_light), join(root, 'escape.png'))
  expect(() => readAsset(root, 'logo_light', 'escape.png')).toThrow(/escapes/)
  for (const bytes of [
    Buffer.from('<svg/>'),
    readFileSync(join(root, refs.logo_light)).subarray(0, 50)
  ]) {
    writeFileSync(join(root, 'bad.png'), bytes)
    expect(() => readAsset(root, 'logo_light', 'bad.png')).toThrow()
  }
  const large = Buffer.from(readFileSync(join(root, refs.icon_master)))
  large.writeUInt32BE(4096, 16)
  writeFileSync(join(root, 'large.png'), large)
  expect(() => readAsset(root, 'icon_master', 'large.png')).toThrow(/dimensions/)
  writeFileSync(join(root, 'invisible.png'), PNG.sync.write(new PNG({ width: 32, height: 32 })))
  expect(() => readAsset(root, 'logo_light', 'invisible.png')).toThrow(/transparent/)
  writeFileSync(
    join(root, 'trailing.png'),
    Buffer.concat([readFileSync(join(root, refs.logo_light)), Buffer.from('trailer')])
  )
  expect(() => readAsset(root, 'logo_light', 'trailing.png')).toThrow(/boundary/)
})
