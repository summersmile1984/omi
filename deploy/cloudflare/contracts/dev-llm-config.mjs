import { lstatSync, readFileSync } from "node:fs";
import { parseEnv } from "node:util";
import { devLlmConfig } from "./dev-llm.mjs";
import { devSpeechConfig, devEmbeddingConfig } from "./dev-media.mjs";

export function readDevLlmVars(path) {
  const stat = lstatSync(path);
  if (
    !stat.isFile() ||
    stat.isSymbolicLink() ||
    stat.size > 16384 ||
    stat.mode & 0o077
  )
    throw new Error("LLM dev vars must be a private ordinary file (mode 0600)");
  const values = parseEnv(readFileSync(path, "utf8"));
  const allowed = [
    "DEV_LLM_URL",
    "DEV_LLM_API_KEY",
    "DEV_LLM_MODEL",
    "DEV_LLM_PROTOCOL",
    "DEV_TTS_VOICE",
    "DEV_OLLAMA_URL",
    "DEV_EMBEDDING_MODEL",
  ];
  if (Object.keys(values).some((key) => !allowed.includes(key)))
    throw new Error("LLM dev vars contain an unsupported setting");
  const config = devLlmConfig(values);
  const embedding = devEmbeddingConfig(values);
  const speech =
    config.protocol === "mimo" ? devSpeechConfig(values) : undefined;
  return {
    secrets: values,
    evidence: {
      mode: "external-llm",
      endpoint: config.url,
      model: config.model,
      protocol: config.protocol,
      embedding: {
        provider: "ollama",
        endpoint: embedding.url,
        model: embedding.model,
        dimensions: 1024,
      },
      asr: speech?.asr ?? "unconfigured",
      tts: speech?.tts ?? "unconfigured",
      voice: speech?.voice,
      vector_index: "transient",
    },
  };
}
