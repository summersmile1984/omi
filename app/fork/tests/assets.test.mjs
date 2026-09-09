import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { PNG } from "../../../scripts/brand/raster/png.mjs";
import { writeFixtureAssets } from "../../../scripts/brand/raster/fixture.mjs";
import { generateFlutterAssets } from "../assets.mjs";

const references = (variant) =>
  Object.fromEntries(
    ["icon_master", "logo_light", "logo_dark", "splash"].map((name) => [
      name,
      `assets/${variant}-${name}.png`,
    ]),
  );

test("Flutter runtime and every Android density decode at their consumer dimensions", () => {
  const root = mkdtempSync(join(tmpdir(), "flutter-brand-input-"));
  const app = mkdtempSync(join(tmpdir(), "flutter-brand-output-"));
  try {
    writeFixtureAssets(root, "harbor");
    const proof = generateFlutterAssets(root, references("harbor"), app);
    assert.equal(Object.keys(proof.outputs).length, 71);
    for (const [path, expected] of Object.entries(proof.outputs)) {
      const bytes = readFileSync(join(app, path));
      const image = PNG.sync.read(bytes, { checkCRC: true });
      assert.deepEqual(
        [image.width, image.height],
        [expected.width, expected.height],
        path,
      );
    }
    assert.deepEqual(
      [
        proof.outputs["android/app/src/main/res/mipmap-mdpi/ic_launcher.png"]
          .width,
        proof.outputs["android/app/src/main/res/mipmap-xxxhdpi/ic_launcher.png"]
          .width,
      ],
      [48, 192],
    );
    assert.deepEqual(
      [
        proof.outputs["android/app/src/main/res/drawable-mdpi/splash_icon.png"]
          .width,
        proof.outputs[
          "android/app/src/main/res/drawable-xxxhdpi/splash_icon.png"
        ].width,
      ],
      [288, 1152],
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
    rmSync(app, { recursive: true, force: true });
  }
});

test("invalid splash prevents every platform replacement", () => {
  const root = mkdtempSync(join(tmpdir(), "flutter-brand-invalid-"));
  const app = mkdtempSync(join(tmpdir(), "flutter-brand-rejected-"));
  try {
    writeFixtureAssets(root, "harbor");
    writeFileSync(
      join(root, "assets/harbor-splash.png"),
      Buffer.from("invalid"),
    );
    assert.throws(
      () => generateFlutterAssets(root, references("harbor"), app),
      /static PNG/,
    );
    assert.throws(
      () => readFileSync(join(app, "assets/images/herologo.png")),
      /ENOENT/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
    rmSync(app, { recursive: true, force: true });
  }
});
