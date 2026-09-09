import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { PNG } from "./png.mjs";

// Deterministic synthetic pixels for engineering fixtures, never a real brand.
export function writeFixtureAssets(root, variant = "harbor") {
  mkdirSync(join(root, "assets"), { recursive: true });
  for (const role of ["icon_master", "logo_light", "logo_dark", "splash"]) {
    const [width, height] =
      role === "icon_master"
        ? [1024, 1024]
        : role === "splash"
          ? [390, 844]
          : [128, 128];
    const png = new PNG({ width, height });
    for (let y = 0; y < height; y++)
      for (let x = 0; x < width; x++) {
        const u = x / width,
          v = y / height,
          i = (y * width + x) * 4;
        const upright = (u > 0.23 && u < 0.37) || (u > 0.63 && u < 0.77);
        const bar =
          variant === "harbor" ? v > 0.43 && v < 0.57 : Math.abs(v - u) < 0.09;
        const mark =
          v > 0.2 && v < 0.8 && (upright || (u > 0.23 && u < 0.77 && bar));
        const circle =
          role === "icon_master" && Math.hypot(u - 0.5, v - 0.5) < 0.47;
        const splash = role === "splash";
        const color =
          role === "logo_light" || (splash && mark) || !mark ? 255 : 0;
        if (splash)
          png.data.set(
            [mark ? 255 : 0, mark ? 255 : 0, mark ? 255 : 0, 255],
            i,
          );
        else if (mark || circle) png.data.set([color, color, color, 255], i);
      }
    writeFileSync(
      join(root, "assets", `${variant}-${role}.png`),
      PNG.sync.write(png),
    );
  }
}
if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
)
  writeFixtureAssets(process.argv[2], process.argv[3]);
