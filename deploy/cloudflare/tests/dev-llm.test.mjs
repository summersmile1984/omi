import { mkdtempSync, writeFileSync, rmSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { devLlmConfig, runDevLlm } from "../contracts/dev-llm.mjs";
import { readDevLlmVars } from "../contracts/dev-llm-config.mjs";

const env = {
  DEV_LLM_URL: "https://llm.example.test/v1/chat/completions",
  DEV_LLM_API_KEY: "private-test-credential",
  DEV_LLM_MODEL: "test-model",
  DEV_OLLAMA_URL: "http://127.0.0.1:11434/api/embed",
  DEV_EMBEDDING_MODEL: "bge-m3",
};
const usage = { prompt_tokens: 14, completion_tokens: 5, total_tokens: 19 };
const messages = [
  { role: "system", content: "Original prompt 中文\nunchanged." },
  { role: "user", content: "你好" },
];
const completion = (
  message = { content: "真实格式回答" },
  finish_reason = "stop"
) => ({
  model: "test-model",
  choices: [{ message, finish_reason }],
  usage,
});
const dirs = [];
afterEach(() =>
  dirs.splice(0).forEach((dir) => rmSync(dir, { recursive: true, force: true }))
);

describe("explicit local LLM binding adapter", () => {
  it("preserves messages/options, tool data and actual usage across OpenAI/Workers AI shapes", async () => {
    const data = completion({
      content: "你好",
      tool_calls: [
        { id: "call", function: { name: "lookup", arguments: "{}" } },
      ],
      reasoning_content: "provider reason",
    });
    const fetcher = vi.fn(async () => Response.json(data));
    const input = {
      messages,
      max_tokens: 123,
      temperature: 0,
      tools: [{ type: "function", function: { name: "lookup" } }],
    };
    const before = structuredClone(input);
    const result = await runDevLlm(env, input, fetcher);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe(env.DEV_LLM_URL);
    expect(init.redirect).toBe("manual");
    expect(init.headers.authorization).toBe(`Bearer ${env.DEV_LLM_API_KEY}`);
    expect(JSON.parse(init.body)).toEqual({
      ...input,
      model: "test-model",
      stream: false,
    });
    expect(input).toEqual(before);
    expect(result).toEqual({ ...data, response: "你好" });
  });
  it("maps MiMo structured output using the caller schema without rewriting prompts", async () => {
    const schema = {
      name: "advice",
      strict: true,
      schema: {
        type: "object",
        properties: { advice: { type: "string" } },
        required: ["advice"],
        additionalProperties: false,
      },
    };
    const data = completion(
      {
        content: null,
        tool_calls: [
          { function: { name: "advice", arguments: '{"advice":"下一步"}' } },
        ],
      },
      "tool_calls"
    );
    const fetcher = vi.fn(async () => Response.json(data));
    const result = await runDevLlm(
      { ...env, DEV_LLM_PROTOCOL: "mimo" },
      {
        messages,
        response_format: { type: "json_schema", json_schema: schema },
        max_tokens: 123,
        temperature: 0,
      },
      fetcher
    );
    const body = JSON.parse(fetcher.mock.calls[0][1].body);
    expect(body.messages).toEqual(messages);
    expect(body.tools).toEqual([
      {
        type: "function",
        function: {
          name: schema.name,
          parameters: schema.schema,
          strict: true,
        },
      },
    ]);
    expect(body).toMatchObject({
      max_completion_tokens: 123,
      thinking: { type: "disabled" },
      tool_choice: "auto",
      temperature: 0,
    });
    expect(body).not.toHaveProperty("max_tokens");
    expect(body).not.toHaveProperty("response_format");
    expect(result.response).toBe('{"advice":"下一步"}');
  });
  it.each([302, 401, 429, 503])(
    "propagates HTTP %s without exposing provider error content or falling back",
    async (status) => {
      const fetcher = vi.fn(
        async () => new Response(env.DEV_LLM_API_KEY, { status })
      );
      await expect(runDevLlm(env, { messages }, fetcher)).rejects.toThrow(
        `development LLM HTTP ${status}`
      );
      expect(fetcher).toHaveBeenCalledOnce();
    }
  );
  it("sanitizes transport failures", async () => {
    await expect(
      runDevLlm(env, { messages }, async () => {
        throw new Error(env.DEV_LLM_API_KEY);
      })
    ).rejects.toThrow("development LLM transport failed");
  });
  it.each([
    { choices: [] },
    completion({ content: "truncated" }, "length"),
    { ...completion(), usage: undefined },
    completion({ content: null }),
  ])(
    "rejects malformed, empty, unmetered or truncated results",
    async (data) => {
      await expect(
        runDevLlm(env, { messages }, async () => Response.json(data))
      ).rejects.toThrow("development LLM");
    }
  );
  it("requires the structured function result instead of accepting unrelated text", async () => {
    await expect(
      runDevLlm(
        { ...env, DEV_LLM_PROTOCOL: "mimo" },
        {
          messages,
          response_format: {
            type: "json_schema",
            json_schema: { name: "advice", schema: { type: "object" } },
          },
        },
        async () => Response.json(completion())
      )
    ).rejects.toThrow("requested structured result");
  });
  it.each([
    { stream: true },
    { audio: [0] },
    { text: ["embedding"] },
    { image: [0] },
  ])("does not treat unsupported model IO as chat", async (options) => {
    const fetcher = vi.fn();
    await expect(
      runDevLlm(env, { messages, ...options }, fetcher)
    ).rejects.toThrow("non-streaming chat");
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("requires explicit private config and excludes credentials from evidence", () => {
    const dir = mkdtempSync(resolve(tmpdir(), "dev-llm-vars-"));
    dirs.push(dir);
    const path = resolve(dir, ".dev.vars");
    writeFileSync(
      path,
      Object.entries(env)
        .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
        .join("\n"),
      { mode: 0o600 }
    );
    const result = readDevLlmVars(path);
    expect(result.secrets).toEqual(env);
    expect(result.evidence).toMatchObject({
      mode: "external-llm",
      embedding: { provider: "ollama", model: "bge-m3", dimensions: 1024 },
      model: "test-model",
    });
    expect(JSON.stringify(result.evidence)).not.toContain(env.DEV_LLM_API_KEY);
    chmodSync(path, 0o644);
    expect(() => readDevLlmVars(path)).toThrow("private ordinary file");
  });
  it.each([
    "http://outside.example/v1/chat/completions",
    "https://key@example.test/v1/chat/completions",
    "https://example.test/v1/chat/completions?key=secret",
  ])("rejects unsafe endpoint %s", (url) => {
    expect(() => devLlmConfig({ ...env, DEV_LLM_URL: url })).toThrow();
  });
});
