import { WorkerEntrypoint } from "cloudflare:workers";

// Controlled inference only: no application, identity, queue or storage binding.
// Real Python Workers own validation, accounting and every persisted result.
export class Provider extends WorkerEntrypoint<{
  INFERENCE_CONTROL_ORIGIN: string;
}> {
  async run(_model: string, input: Record<string, unknown>) {
    if (!input.response_format) {
      const messages = input.messages as { role: string; content: string }[];
      const reference = messages
        .filter((message) => message.role === "user")
        .map((message) => message.content);
      if (reference.at(-1) === "[fixture:provider-error]")
        throw new Error("controlled inference failure");
      const gate = /^\[fixture:wait:([0-9a-f-]{36})\]$/.exec(
        reference.at(-1) ?? "",
      );
      if (gate) {
        const response = await fetch(
          `${this.env.INFERENCE_CONTROL_ORIGIN}/wait/${gate[1]}`,
          {
            signal: AbortSignal.timeout(20000),
          },
        );
        if (!response.ok) throw new Error("controlled inference wait failed");
      }
      return {
        response: "Synthetic chat 回答\n" + reference.join("\n"),
        usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 },
      };
    }
    return {
      response: JSON.stringify({
        title: "Synthetic recording",
        overview: "Local provider-controlled recording contract.",
        emoji: "📝",
        category: "other",
        discarded: false,
        action_items: [{ description: "Send the synthetic follow-up" }],
        memories: ["The user prefers concise updates."],
      }),
    };
  }
}
export default {
  fetch() {
    return new Response("local inference fixture");
  },
};
