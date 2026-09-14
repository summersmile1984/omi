import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { PNG, readAsset, resize } from "../../../scripts/brand/raster/png.mjs";

const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");

export function generateMacAssets(root, refs, output) {
  // Resolve and decode every consumed input before replacing a staged resource.
  const inputs = Object.fromEntries(
    ["icon_master", "logo_light", "logo_dark"].map((name) => [
      name,
      readAsset(root, name, refs[name]),
    ]),
  );
  const outputs = {};
  function write(path, bytes) {
    const destination = join(output, path);
    mkdirSync(resolve(destination, ".."), { recursive: true });
    writeFileSync(destination, bytes);
    outputs[path] = { sha256: digest(bytes), bytes: bytes.length };
  }

  const resources = "Desktop/Sources/Resources/";
  for (const [name, role, size] of [
    ["omi_app_icon.png", "icon_master", 1024],
    ["omi_menu_bar_icon.png", "logo_light", 64],
    ["herologo.png", "logo_light", 256],
    ["ForkBrandLight.png", "logo_light", 256],
    ["ForkBrandDark.png", "logo_dark", 256],
  ])
    write(resources + name, PNG.sync.write(resize(inputs[role].png, size)));

  const iconset = join(output, "ForkAppIcon.iconset");
  mkdirSync(iconset);
  for (const size of [16, 32, 128, 256, 512])
    for (const scale of [1, 2])
      writeFileSync(
        join(iconset, `icon_${size}x${size}${scale === 2 ? "@2x" : ""}.png`),
        PNG.sync.write(resize(inputs.icon_master.png, size * scale)),
      );
  const icns = join(output, "ForkAppIcon.icns");
  execFileSync("/usr/bin/iconutil", ["-c", "icns", iconset, "-o", icns]);
  const icnsBytes = readFileSync(icns);
  outputs["ForkAppIcon.icns"] = {
    sha256: digest(icnsBytes),
    bytes: icnsBytes.length,
  };

  const coverage = {
    inputs: Object.fromEntries(
      Object.entries(inputs).map(([name, { png, ...value }]) => [
        name,
        { ...value, reference: refs[name] },
      ]),
    ),
    outputs,
    unconsumed: {
      splash: "No selected splash consumer in this local macOS package",
      other_macos_marks:
        "Cinematic, notch, chat inline marks, text logo, videos and legacy assets require separate consumer packages",
    },
    qualification:
      "Local PNG and ICNS resource generation only; no release signing, supported OS floor or installed application proof",
  };
  writeFileSync(
    join(output, "brand-assets.json"),
    JSON.stringify(coverage, null, 2) + "\n",
  );
  return coverage;
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  const [root, input, output] = process.argv.slice(2);
  generateMacAssets(root, JSON.parse(readFileSync(input, "utf8")), output);
}
