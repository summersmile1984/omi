import { WorkerEntrypoint } from "cloudflare:workers";
import { runDevLlm } from "./dev-llm.mjs";
import { runDevAsr, runDevTts, runDevEmbedding } from "./dev-media.mjs";
export { MemoryVectors } from "./provider-fixture";

// Selected only by the disposable local target. No production config imports it.
export class Provider extends WorkerEntrypoint {
  async run(
    model: string,
    input: Record<string, unknown>,
    options?: { returnRawResponse?: boolean }
  ) {
    try {
      if (model === "@cf/baai/bge-m3")
        return await runDevEmbedding(this.env, input);
      if (typeof input.audio === "string")
        return await runDevAsr(this.env, input);
      if (typeof input.text === "string") {
        const audio = await runDevTts(this.env, input);
        return options?.returnRawResponse
          ? new Response(audio.bytes, {
              headers: { "content-type": "audio/mpeg" },
            })
          : {
              state: "Completed",
              result: { audio: `data:audio/mpeg;base64,${audio.encoded}` },
            };
      }
      const result = await runDevLlm(this.env, input);
      console.log(
        JSON.stringify({
          event: "dev_llm_completion",
          usage: {
            prompt_tokens: result.usage.prompt_tokens,
            completion_tokens: result.usage.completion_tokens,
            total_tokens: result.usage.total_tokens,
          },
        })
      );
      return result;
    } catch (error) {
      // Adapter errors contain fixed protocol/status messages, never upstream
      // response bodies, prompts, credentials or account identifiers.
      const message = error instanceof Error ? error.message : "unknown";
      console.error(
        JSON.stringify({
          event: "dev_llm_failure",
          reason:
            /^(development AI|development LLM|DEV_LLM_|invalid development|MiMo supports)/.test(
              message
            )
              ? message
              : "adapter failure",
        })
      );
      throw error;
    }
  }
}

export default {
  fetch() {
    return new Response("local external LLM adapter");
  },
};
