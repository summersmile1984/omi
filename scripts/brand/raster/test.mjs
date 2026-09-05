import assert from "node:assert/strict";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { PNG, readAsset, resize } from "./png.mjs";
import { writeFixtureAssets } from "./fixture.mjs";

function temporary() {
  return mkdtempSync(join(tmpdir(), "brand-raster-contract-"));
}

test("synthetic manifest rasters decode and resize through the shared production boundary", () => {
  const root = temporary();
  try {
    writeFixtureAssets(root, "harbor");
    const icon = readAsset(
      root,
      "icon_master",
      "assets/harbor-icon_master.png",
    );
    const splash = readAsset(root, "splash", "assets/harbor-splash.png");
    assert.deepEqual([icon.width, icon.height], [1024, 1024]);
    assert.deepEqual([splash.width, splash.height], [390, 844]);
    const rendered = resize(icon.png, 64);
    const roundTrip = PNG.sync.read(PNG.sync.write(rendered), {
      checkCRC: true,
    });
    assert.deepEqual([roundTrip.width, roundTrip.height], [64, 64]);
    assert.ok(
      roundTrip.data.some((value, index) => index % 4 === 3 && value > 0),
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("corrupt, animated, transparent and escaping inputs fail closed", () => {
  const root = temporary();
  const outside = temporary();
  try {
    writeFixtureAssets(root);
    writeFileSync(join(root, "corrupt.png"), Buffer.from("not a png"));
    assert.throws(
      () => readAsset(root, "logo_light", "corrupt.png"),
      /static PNG/,
    );

    const transparent = new PNG({ width: 32, height: 32 });
    writeFileSync(join(root, "transparent.png"), PNG.sync.write(transparent));
    assert.throws(
      () => readAsset(root, "logo_light", "transparent.png"),
      /fully transparent/,
    );

    const original = readFileSync(join(root, "assets/harbor-logo_light.png"));
    const animated = Buffer.concat([
      original.subarray(0, 33),
      Buffer.from([0, 0, 0, 0, 0x61, 0x63, 0x54, 0x4c, 0, 0, 0, 0]),
      original.subarray(33),
    ]);
    writeFileSync(join(root, "animated.png"), animated);
    assert.throws(
      () => readAsset(root, "logo_light", "animated.png"),
      /animated PNG/,
    );

    writeFileSync(join(outside, "outside.png"), original);
    mkdirSync(join(root, "linked"));
    symlinkSync(join(outside, "outside.png"), join(root, "linked/logo.png"));
    assert.throws(
      () => readAsset(root, "logo_light", "linked/logo.png"),
      /escapes/,
    );
    assert.throws(
      () => readAsset(root, "logo_light", "../outside.png"),
      /relative file/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
    rmSync(outside, { recursive: true, force: true });
  }
});
