import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { PNG } from "../../../../scripts/brand/raster/png.mjs";
import { writeFixtureAssets } from "../../../../scripts/brand/raster/fixture.mjs";
import { generateMacAssets } from "../assets.mjs";

const refs = (variant) =>
  Object.fromEntries(
    ["icon_master", "logo_light", "logo_dark"].map((name) => [
      name,
      `assets/${variant}-${name}.png`,
    ]),
  );

test("macOS resources and icon container derive from one validated manifest asset set", () => {
  const root = mkdtempSync(join(tmpdir(), "mac-brand-input-"));
  const output = mkdtempSync(join(tmpdir(), "mac-brand-output-"));
  try {
    writeFixtureAssets(root, "harbor");
    const proof = generateMacAssets(root, refs("harbor"), output);
    const sizes = {
      "omi_app_icon.png": 1024,
      "omi_menu_bar_icon.png": 64,
      "herologo.png": 256,
      "ForkBrandLight.png": 256,
      "ForkBrandDark.png": 256,
    };
    for (const [name, size] of Object.entries(sizes)) {
      const path = join(output, "Desktop/Sources/Resources", name);
      const image = PNG.sync.read(readFileSync(path), { checkCRC: true });
      assert.deepEqual([image.width, image.height], [size, size]);
      assert.equal(
        proof.outputs[`Desktop/Sources/Resources/${name}`].bytes,
        readFileSync(path).length,
      );
    }
    const icns = readFileSync(join(output, "ForkAppIcon.icns"));
    assert.equal(icns.toString("ascii", 0, 4), "icns");
    assert.equal(proof.outputs["ForkAppIcon.icns"].sha256.length, 64);
  } finally {
    rmSync(root, { recursive: true, force: true });
    rmSync(output, { recursive: true, force: true });
  }
});

test("macOS generation does not replace resources when one required input is invalid", () => {
  const root = mkdtempSync(join(tmpdir(), "mac-brand-invalid-"));
  const output = mkdtempSync(join(tmpdir(), "mac-brand-rejected-"));
  try {
    writeFixtureAssets(root, "harbor");
    writeFileSync(
      join(root, "assets/harbor-logo_dark.png"),
      Buffer.from("invalid"),
    );
    assert.throws(
      () => generateMacAssets(root, refs("harbor"), output),
      /static PNG/,
    );
    assert.throws(
      () =>
        readFileSync(
          join(output, "Desktop/Sources/Resources/omi_app_icon.png"),
        ),
      /ENOENT/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
    rmSync(output, { recursive: true, force: true });
  }
});
