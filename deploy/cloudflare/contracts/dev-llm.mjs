// Local provider protocol adapter. This module is never imported by a production Worker.
export function devLlmConfig(env) {
  let url;
  try {
    url = new URL(env.DEV_LLM_URL);
  } catch {
    throw new Error(
      "DEV_LLM_URL must be an explicit chat/completions endpoint"
    );
  }
  if (
    (url.protocol !== "https:" &&
      !(
        url.protocol === "http:" &&
        ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      )) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !url.pathname.endsWith("/chat/completions")
  )
    throw new Error(
      "development LLM requires HTTPS or loopback HTTP without URL credentials"
    );
  const model = env.DEV_LLM_MODEL;
  const protocol = env.DEV_LLM_PROTOCOL ?? "openai";
  if (typeof model !== "string" || !/^[\w@./:-]{1,150}$/.test(model))
    throw new Error("DEV_LLM_MODEL must be explicit");
  if (!["openai", "mimo"].includes(protocol))
    throw new Error("DEV_LLM_PROTOCOL must be openai or mimo");
  const key = env.DEV_LLM_API_KEY ?? "";
  if (typeof key !== "string" || /[\r\n]/.test(key))
    throw new Error("invalid development LLM credential");
  return { url: url.href, model, protocol, key };
}

export async function runDevLlm(env, input, fetcher = fetch) {
  const config = devLlmConfig(env);
  if (
    !input ||
    typeof input !== "object" ||
    input.stream ||
    input.audio ||
    input.image ||
    input.text
  )
    throw new Error("development LLM supports non-streaming chat inputs only");
  const messages =
    input.messages ??
    (typeof input.prompt === "string"
      ? [{ role: "user", content: input.prompt }]
      : undefined);
  if (!Array.isArray(messages) || !messages.length)
    throw new Error("development LLM requires messages or a text prompt");
  const body = { ...input, messages, model: config.model, stream: false };
  delete body.prompt;
  let structuredName;
  if (config.protocol === "mimo") {
    // MiMo's documented protocol uses max_completion_tokens and only auto tool
    // choice. Keep caller messages byte-for-byte; do not add a JSON instruction.
    body.thinking = { type: "disabled" };
    if (body.max_tokens !== undefined) {
      body.max_completion_tokens ??= body.max_tokens;
      delete body.max_tokens;
    }
    delete body.reasoning_effort;
    if (body.n !== undefined && body.n !== 1)
      throw new Error("development MiMo adapter requires n=1");
    delete body.n;
    if (body.response_format?.type === "json_schema") {
      const schema = body.response_format.json_schema;
      if (body.tools || !schema?.schema || !/^[\w-]{1,64}$/.test(schema.name))
        throw new Error("invalid development structured-output request");
      structuredName = schema.name;
      body.tools = [
        {
          type: "function",
          function: {
            name: structuredName,
            parameters: schema.schema,
            strict: schema.strict ?? true,
          },
        },
      ];
      body.tool_choice = "auto";
      delete body.response_format;
    } else if (body.tool_choice && body.tool_choice !== "auto") {
      throw new Error("MiMo supports auto tool choice only");
    }
  }
  let response;
  try {
    response = await fetcher(config.url, {
      method: "POST",
      // The pinned workerd rejects Node's redirect: "error". A 3xx
      // response fails below, so credentials never follow a redirect.
      redirect: "manual",
      signal: AbortSignal.timeout(180000),
      headers: {
        "content-type": "application/json",
        ...(config.key ? { authorization: `Bearer ${config.key}` } : {}),
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error("development LLM transport failed");
  }
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error(`development LLM HTTP ${response.status}`);
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("development LLM returned invalid JSON");
  }
  const choice = data?.choices?.[0];
  const message = choice?.message;
  if (!message || !["stop", "tool_calls"].includes(choice.finish_reason))
    throw new Error("development LLM returned an incomplete completion");
  let text = message.content;
  if (structuredName) {
    const calls = message.tool_calls;
    if (calls?.length !== 1 || calls[0].function?.name !== structuredName)
      throw new Error(
        "development LLM did not return the requested structured result"
      );
    text = calls[0].function.arguments;
    try {
      const value = JSON.parse(text);
      if (!value || typeof value !== "object" || Array.isArray(value))
        throw new Error();
    } catch {
      throw new Error("development LLM returned invalid structured arguments");
    }
  }
  if (!(typeof text === "string" && text.trim()) && !message.tool_calls?.length)
    throw new Error("development LLM returned no answer");
  const usage = data.usage;
  if (
    !usage ||
    ["prompt_tokens", "completion_tokens", "total_tokens"].some(
      (key) => !Number.isSafeInteger(usage[key]) || usage[key] < 0
    )
  )
    throw new Error("development LLM returned no valid usage");
  // Keep OpenAI choices (including tool/reasoning data) for compatible callers,
  // and Workers AI's response/usage shape for existing Python and TS owners.
  return { ...data, response: text ?? "", usage };
}
