// Wrangler-only runtime entrypoint. The package's exported types remain the
// authority for sync-audio.ts, while this module avoids its optional Worker.
// @ts-expect-error The package does not publish declarations for its core subpath.
import OpusDecoder from "../../node_modules/opus-decoder/src/OpusDecoder.js";

export { OpusDecoder };
