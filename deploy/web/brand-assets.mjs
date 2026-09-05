import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PNG, readAsset, resize } from '../../scripts/brand/raster/png.mjs';

const roles = ['icon_master', 'logo_light'];
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
function inputs(input) {
  if (!input?.root || !input?.refs)
    throw new Error('Web brand assets require manifest-relative inputs');
  return Object.fromEntries(
    roles.map((role) => [role, readAsset(input.root, role, input.refs[role])]),
  );
}

/** @returns {{mode: string, inputs: Record<string, unknown>, outputs: Record<string, {role: string, sha256: string, bytes: number, width: number, height: number}>}} */
export function generateWebAssets(brandId, input, publicDirectory) {
  // The regression identity keeps upstream resources, just like brand/apply.py.
  if (brandId === 'omi-upstream') return { mode: 'upstream', inputs: {}, outputs: {} };
  const decoded = inputs(input);
  const outputs = {};
  for (const [path, role, size] of [
    ['logo.png', 'logo_light', 512],
    ['favicon.png', 'icon_master', 64],
  ]) {
    const bytes = PNG.sync.write(resize(decoded[role].png, size));
    mkdirSync(publicDirectory, { recursive: true });
    writeFileSync(resolve(publicDirectory, path), bytes);
    outputs[path] = {
      role,
      sha256: digest(bytes),
      bytes: bytes.length,
      width: size,
      height: size,
    };
  }
  return {
    mode: 'manifest',
    inputs: Object.fromEntries(
      Object.entries(decoded).map(([role, { png, ...receipt }]) => [role, receipt]),
    ),
    outputs,
  };
}

// Private manifests are snapshotted beside their consumed PNGs before either
// target builds. Preserve their relative references and verify original bytes.
export function snapshotWebAssets(brandId, input, output) {
  if (brandId === 'omi-upstream') return;
  const decoded = inputs(input);
  for (const role of roles) {
    const reference = input.refs[role];
    const destination = resolve(output, reference);
    if (
      ['manifest.json', 'inventory.json'].some(
        (name) => destination === resolve(output, name),
      )
    )
      throw new Error('Web asset collides with release input owner');
    const bytes = readFileSync(resolve(input.root, reference));
    if (digest(bytes) !== decoded[role].sha256)
      throw new Error('Web brand asset changed during snapshot');
    mkdirSync(dirname(destination), { recursive: true });
    writeFileSync(destination, bytes, { mode: 0o600 });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.length !== 4 || process.argv[2] !== '--snapshot')
    throw new Error('Expected --snapshot OUTPUT');
  const input = JSON.parse(readFileSync(0, 'utf8'));
  snapshotWebAssets(input.brand_id, input.asset_input, resolve(process.argv[3]));
}
