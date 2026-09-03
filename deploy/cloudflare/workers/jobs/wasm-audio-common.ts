// The public package entrypoint eagerly imports its optional Web Worker class,
// which cannot exist in a Cloudflare isolate. Wrangler aliases only the pinned
// runtime build to this non-worker entrypoint; Vitest still uses package types.
// @ts-expect-error The package does not publish declarations for its core subpath.
export { default as WASMAudioDecoderCommon } from "../../node_modules/@wasm-audio-decoders/common/src/WASMAudioDecoderCommon.js";
