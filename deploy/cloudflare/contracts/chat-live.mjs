import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { build } from "esbuild";
import { runLiveMedia } from "./media-live.mjs";

// The existing product runner selects this case set only with explicit live
// credentials. It never substitutes nondeterministic inference for CI faults.
const { values } = parseArgs({ options: { metadata: { type: "string" } } });
const metadata = JSON.parse(readFileSync(values.metadata));
const fixture = JSON.parse(
  readFileSync(resolve(dirname(values.metadata), "fixture.json"))
);
assert.equal(fixture.inference?.mode, "external-llm");
for (const origin of [metadata.api_origin, metadata.auth_origin]) {
  const url = new URL(origin);
  assert(
    url.protocol === "http:" &&
      url.hostname === "127.0.0.1" &&
      url.port &&
      url.pathname === "/" &&
      !url.search &&
      !url.hash &&
      !url.username &&
      !url.password
  );
}
mkdirSync(metadata.trace_dir, { recursive: true, mode: 0o700 });
const report = {
  schema_version: 1,
  target: metadata.target,
  brand_id: metadata.brand_id,
  scope: "external-llm-chat",
  inference: fixture.inference,
  cases: [],
  release_qualified: false,
};
const originalFetch = globalThis.fetch;
const sessions = [];
let token;
const pass = (id) => report.cases.push({ id, result: "pass" });
async function request(
  path,
  { bearer, method = "GET", body, status = 200, retry = true } = {}
) {
  const response = await originalFetch(metadata.api_origin + path, {
    method,
    redirect: "error",
    signal: AbortSignal.timeout(240000),
    headers: {
      "content-type": "application/json",
      origin: metadata.auth_origin,
      ...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  // The common suite runs first and shares the local signup rate limit. Honor
  // the same server-owned retry interval as the deterministic chat consumer.
  if (path === "/api/auth/sign-up/email" && response.status === 429 && retry) {
    const seconds = Number(response.headers.get("x-retry-after"));
    assert(Number.isInteger(seconds) && seconds > 0 && seconds <= 60);
    await response.body?.cancel();
    await new Promise((done) => setTimeout(done, seconds * 1000));
    return request(path, { bearer, method, body, status, retry: false });
  }
  assert.equal(response.status, status, `${method} ${path}`);
  return response;
}
async function signup() {
  const response = await request("/api/auth/sign-up/email", {
    method: "POST",
    body: {
      email: `llm-${randomUUID()}@example.invalid`,
      password: randomUUID() + randomUUID(),
      name: "Live LLM Contract",
    },
  });
  const session = response.headers.get("set-auth-token");
  assert(session);
  sessions.push(session);
  return (await (await request("/api/auth/token", { bearer: session })).json())
    .token;
}
try {
  const root = resolve(import.meta.dirname, "../../..");
  const compiled = await build({
    entryPoints: [resolve(root, "web/app/src/lib/api.ts")],
    bundle: true,
    platform: "browser",
    format: "esm",
    write: false,
    plugins: [
      {
        name: "public-auth-token",
        setup(build) {
          build.onResolve({ filter: /^\.\/firebase$/ }, () => ({
            path: "token",
            namespace: "fixture",
          }));
          build.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({
            contents:
              "export const getIdToken = () => globalThis.__liveLlmToken();",
            loader: "js",
          }));
        },
      },
    ],
  });
  const module = resolve(metadata.trace_dir, "upstream-chat-client.mjs");
  writeFileSync(module, compiled.outputFiles[0].contents, { mode: 0o600 });
  globalThis.__liveLlmToken = async () => token;
  globalThis.fetch = async (input, init) => {
    assert(typeof input === "string" && input.startsWith("/api/proxy/"));
    return originalFetch(
      metadata.api_origin + input.slice("/api/proxy".length),
      { ...init, redirect: "error", signal: AbortSignal.timeout(240000) }
    );
  };
  const client = await import(pathToFileURL(module));
  const owner = await signup();
  token = owner;
  const session = (
    await (
      await request("/v2/chat-sessions", {
        bearer: owner,
        method: "POST",
        body: { title: "Live LLM verification" },
      })
    ).json()
  ).id;
  const chunks = [];
  await client.sendMessageStream(
    "请用中文给我两条今天整理工作笔记的简短建议。",
    (chunk) => chunks.push(chunk),
    { chatSessionId: session }
  );
  const done = chunks.filter((row) => row.type === "done");
  assert.equal(done.length, 1);
  const answer = done[0].message;
  assert(typeof answer.text === "string" && answer.text.trim().length > 0);
  assert.equal(answer.chat_session_id, session);
  assert.equal(
    chunks
      .filter((row) => row.type === "data")
      .map((row) => row.text)
      .join(""),
    answer.text
  );
  pass("real-llm-upstream-client-sse");
  const history = await client.getMessages(undefined, session);
  assert.equal(history.length, 2);
  assert(
    history.some((row) => row.id === answer.id && row.text === answer.text)
  );
  assert(history.every((row) => row.chat_session_id === session));
  pass("real-llm-persisted-session-history");
  const other = await signup();
  await request(`/v2/messages?chat_session_id=${session}`, {
    bearer: other,
    status: 404,
  });
  pass("real-llm-cross-account-denial");
  const goal = await (
    await request("/v1/goals", {
      bearer: owner,
      method: "POST",
      body: {
        title: "每天整理三条工作笔记",
        goal_type: "numeric",
        target_value: 3,
      },
    })
  ).json();
  const advice = await (
    await request(`/v1/goals/${goal.id}/advice`, { bearer: owner })
  ).json();
  assert(
    typeof advice.advice === "string" &&
      advice.advice.trim() &&
      advice.advice !== "Focus on the next small step toward your goal."
  );
  pass("real-llm-structured-goal-advice");
  await client.clearMessages(undefined, session);
  const cleared = await client.getMessages(undefined, session);
  assert(!cleared.some((row) => row.id === answer.id));
  pass("real-llm-clear-persisted-history");
  if (fixture.inference.asr === "mimo-v2.5-asr")
    await runLiveMedia({
      metadata,
      token: owner,
      fetcher: originalFetch,
      pass,
    });
  report.passed = true;
} finally {
  globalThis.fetch = originalFetch;
  delete globalThis.__liveLlmToken;
  for (const session of sessions) {
    const response = await request("/api/auth/sign-out", {
      bearer: session,
      method: "POST",
      body: {},
    });
    await response.body?.cancel();
  }
  writeFileSync(
    resolve(metadata.trace_dir, "chat-results.json"),
    JSON.stringify(report, null, 2) + "\n",
    { mode: 0o600 }
  );
}
console.log(
  JSON.stringify({
    passed: report.passed,
    cases: report.cases.length,
    model: fixture.inference.model,
  })
);
