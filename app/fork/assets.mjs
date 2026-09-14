import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { PNG, readAsset, resize } from "../../scripts/brand/raster/png.mjs";

const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");
const densities = { mdpi: 1, hdpi: 1.5, xhdpi: 2, xxhdpi: 3, xxxhdpi: 4 };

function solid(size, value = 255) {
  const image = new PNG({ width: size, height: size });
  for (let index = 0; index < image.data.length; index += 4)
    image.data.set([value, value, value, 255], index);
  return image;
}

export function generateFlutterAssets(root, refs, app) {
  // All four manifest inputs decode before any copied upstream asset is replaced.
  const inputs = Object.fromEntries(
    ["icon_master", "logo_light", "logo_dark", "splash"].map((name) => [
      name,
      readAsset(root, name, refs[name]),
    ]),
  );
  const outputs = {};
  function write(path, image) {
    const destination = join(app, path);
    const bytes = PNG.sync.write(image);
    mkdirSync(resolve(destination, ".."), { recursive: true });
    writeFileSync(destination, bytes);
    outputs[path] = {
      sha256: digest(bytes),
      bytes: bytes.length,
      width: image.width,
      height: image.height,
    };
  }

  write(
    "assets/images/app_launcher_icon.png",
    resize(inputs.icon_master.png, 1024),
  );
  write("assets/images/herologo.png", resize(inputs.logo_light.png, 256));
  write("assets/images/splash_icon.png", resize(inputs.logo_light.png, 500));
  write("assets/images/splash.png", inputs.splash.png);
  for (const [density, scale] of Object.entries(densities)) {
    const launcher = Math.round(48 * scale);
    const adaptive = Math.round(108 * scale);
    const notification = Math.round(24 * scale);
    for (const sourceSet of ["main", "dev", "prod"])
      write(
        `android/app/src/${sourceSet}/res/mipmap-${density}/ic_launcher.png`,
        resize(inputs.icon_master.png, launcher),
      );
    write(
      `android/app/src/main/res/mipmap-${density}/ic_launcher_foreground.png`,
      resize(inputs.logo_dark.png, adaptive),
    );
    write(
      `android/app/src/main/res/mipmap-${density}/ic_launcher_background.png`,
      solid(adaptive),
    );
    write(
      `android/app/src/main/res/mipmap-${density}/ic_launcher_monochrome.png`,
      resize(inputs.logo_dark.png, adaptive),
    );
    write(
      `android/app/src/main/res/mipmap-${density}/ic_stat_launcher.png`,
      resize(inputs.logo_light.png, notification),
    );
    const splashSize = Math.round(288 * scale);
    for (const mode of ["", "-night"])
      for (const name of [
        "splash_icon.png",
        "splash_icon_v2.png",
        "android12splash.png",
      ])
        write(
          `android/app/src/main/res/drawable${mode}-${density}/${name}`,
          resize(inputs.logo_light.png, splashSize),
        );
  }
  for (const qualifier of ["drawable", "drawable-v21"])
    write(
      `android/app/src/main/res/${qualifier}/background.png`,
      inputs.splash.png,
    );

  const coverage = {
    inputs: Object.fromEntries(
      Object.entries(inputs).map(([name, { png, ...value }]) => [
        name,
        { ...value, reference: refs[name] },
      ]),
    ),
    outputs,
    consumers: {
      runtime_logo:
        "assets/images/herologo.png through generated Assets and rootBundle callers",
      legacy_launcher:
        "main/dev/prod mipmap ic_launcher; local fork packaging remains debug-only",
      adaptive_launcher:
        "main foreground/background/monochrome XML resource names",
      notification: "main ic_stat_launcher resource name",
      splash:
        "main launch_background and Android 12 splash_icon resource names",
    },
    unconsumed: {
      ios: "iOS catalogs, extensions, App Groups and signing require a separate platform package",
      text_logo:
        "Logo Text White and other branded device/media assets require reviewed consumers",
    },
    qualification:
      "Local Flutter runtime and Android debug resources; no APK, iOS, signing or store proof",
  };
  mkdirSync(join(app, "fork"), { recursive: true });
  writeFileSync(
    join(app, "fork/asset-coverage.json"),
    JSON.stringify(coverage, null, 2) + "\n",
  );
  return coverage;
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  const [root, input, app] = process.argv.slice(2);
  generateFlutterAssets(root, JSON.parse(readFileSync(input, "utf8")), app);
}
