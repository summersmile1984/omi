export type ServingSurface = "streaming" | "prerecorded" | "ptt";

export type ProviderPolicy = {
  surface: ServingSurface;
  model: string;
  provider: string;
  fallback: string[];
};

// This is intentionally small and versioned. CF-08 will generate this module
// from the canonical stt-providers.yaml and update the legacy Python policy in
// the same change, so the two runtimes cannot silently diverge.
export const STT_POLICY_VERSION = "v0-worker-spike";

export const defaultStreamingPolicy: ProviderPolicy = {
  surface: "streaming",
  model: "@cf/deepgram/nova-3",
  provider: "workers-ai",
  fallback: ["deepgram-cloud"],
};
