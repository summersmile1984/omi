import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  resolve: {
    alias: {
      "cloudflare:workers": fileURLToPath(
        new URL("./tests/cloudflare-workers-runtime.ts", import.meta.url),
      ),
    },
  },
  test: {
    include: ["tests/**/*.test.ts", "tests/**/*.test.mjs"],
    environment: "node",
    // Tests also start Python interpreters. CPU count alone (32 on the shared
    // Mac Studio runner) overstates the capacity available to this suite.
    maxWorkers: 4,
  },
});
