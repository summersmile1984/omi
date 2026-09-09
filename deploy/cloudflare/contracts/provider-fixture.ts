import { WorkerEntrypoint } from "cloudflare:workers";

// Controlled inference and memory-index IO, with no application/identity/queue
// binding. The disposable index deliberately has no durability/quality claim.
// Real Python Workers own validation, accounting and every persisted result.
export class Provider extends WorkerEntrypoint<{
  INFERENCE_CONTROL_ORIGIN: string;
}> {
  async run(_model: string, input: Record<string, unknown>) {
    if (Array.isArray(input.text)) {
      return { data: input.text.map(() => Array(1024).fill(0.01)) };
    }
    if (!input.response_format) {
      const messages = input.messages as { role: string; content: string }[];
      const reference = messages
        .filter((message) => message.role === "user")
        .map((message) => message.content);
      if (
        messages[0]?.content.startsWith("You are an expert app designer for ")
      )
        return {
          response: JSON.stringify({
            name: "Omi Research",
            description: messages[0].content.split("\n")[0],
            category: "other",
            capabilities: ["chat"],
            chat_prompt: reference.at(-1),
          }),
        };
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
        response:
          "Synthetic chat 回答\n" +
          (reference.at(-1)?.startsWith("[fixture:brand-prompt]")
            ? messages.map((message) => message.content).join("\n")
            : reference.join("\n")),
        usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 },
      };
    }
    const format = input.response_format as { json_schema?: { name?: string } };
    if (format?.json_schema?.name === "omi_daily_summary") {
      const messages = input.messages as { role: string; content: string }[];
      const context = JSON.parse(
        messages.find((message) => message.role === "user")!.content,
      );
      const source = context.conversations.find(
        (row: { content: string }) => row.content,
      );
      return {
        response: JSON.stringify({
          headline: "Synthetic daily recap",
          overview: "Recap of " + JSON.parse(source.content).overview,
          day_emoji: "📝",
          highlights: [
            {
              topic: "Recording",
              emoji: "🎙️",
              summary: "A recorded follow-up.",
              conversation_numbers: [source.conversation_number],
            },
          ],
          unresolved_questions: [],
          decisions_made: [],
          knowledge_nuggets: [],
        }),
        usage: { prompt_tokens: 100, completion_tokens: 40, total_tokens: 140 },
      };
    }
    if (format?.json_schema?.name === "omi_goal_advice") {
      const messages = input.messages as { role: string; content: string }[];
      return {
        response: JSON.stringify({
          advice: messages.find((message) => message.role === "user")?.content,
        }),
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

const memoryVectors = new Map<string, Record<string, unknown>>();
let processedMemoryMutation = "";

export class MemoryVectors extends WorkerEntrypoint {
  async upsert(vectors: Array<Record<string, unknown>>) {
    for (const vector of vectors) memoryVectors.set(String(vector.id), vector);
    processedMemoryMutation = crypto.randomUUID();
    return { mutationId: processedMemoryMutation };
  }

  async deleteByIds(ids: string[]) {
    for (const id of ids) memoryVectors.delete(id);
    processedMemoryMutation = crypto.randomUUID();
    return { mutationId: processedMemoryMutation };
  }

  async getByIds(ids: string[]) {
    return ids.flatMap((id) => memoryVectors.has(id) ? [memoryVectors.get(id)] : []);
  }

  async describe() {
    return { processedUpToMutation: processedMemoryMutation };
  }

  async query(_vector: number[], options: { namespace: string; topK: number }) {
    const matches = [...memoryVectors.values()]
      .filter((vector) => vector.namespace === options.namespace)
      .sort((left, right) => String(left.id).localeCompare(String(right.id)))
      .slice(0, options.topK)
      .map((vector) => ({ id: vector.id, score: 0.99 }));
    return { count: matches.length, matches };
  }
}

export default {
  fetch() {
    return new Response("local inference fixture");
  },
};
