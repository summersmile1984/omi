import { WorkerEntrypoint } from "cloudflare:workers";

// Controlled inference only: no application, identity, queue or storage binding.
// Real Core owns validation, grounding, finalization and all derived D1 writes.
export class Provider extends WorkerEntrypoint {
  async run() {
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
