import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { build } from "esbuild";
import { fixtureSecret } from "./local-secrets.mjs";

// The real upstream Web API consumer supplies request shape and SSE decoding.
// Seams only provide a real public-auth JWT and the Web proxy's base mapping.
const { values } = parseArgs({ options: { metadata: { type: "string" } } });
const metadata = JSON.parse(readFileSync(values.metadata));
const fixture = JSON.parse(
  readFileSync(resolve(dirname(values.metadata), "fixture.json")),
);
const controlOrigin = fixture.inference_control_origin;
assert.equal(fixture.brand_runtime.brand_id, metadata.brand_id);
for (const origin of [
  metadata.api_origin,
  metadata.auth_origin,
  controlOrigin,
]) {
  const url = new URL(origin);
  assert(
    url.protocol === "http:" &&
      ["127.0.0.1", "localhost"].includes(url.hostname) &&
      url.port &&
      !url.username &&
      !url.password &&
      url.pathname === "/" &&
      !url.search &&
      !url.hash,
    "chat contract requires an explicit loopback target",
  );
}
mkdirSync(metadata.trace_dir, { recursive: true, mode: 0o700 });
const report = {
  schema_version: 1,
  scope: "text-chat-selected-session",
  target: metadata.target,
  brand_id: metadata.brand_id,
  cases: [],
  release_qualified: false,
};
const sessions = [],
  trace = [],
  originalFetch = globalThis.fetch;
let token = null;
async function request(
  service,
  path,
  status,
  { bearer, method = "GET", body, retry = true, extraHeaders = {} } = {},
) {
  const response = await originalFetch(metadata[`${service}_origin`] + path, {
    method,
    redirect: "error",
    signal: AbortSignal.timeout(20000),
    headers: {
      Origin: metadata.auth_origin,
      "Content-Type": "application/json",
      ...extraHeaders,
      ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  trace.push({
    service,
    method,
    route: path
      .replace(/\/shared\/[^/?]+/g, "/shared/:token")
      .replace(/[0-9a-f]{8}-[0-9a-f-]{27}/g, ":id"),
    status: response.status,
    expected: status,
  });
  if (path === "/api/auth/sign-up/email" && response.status === 429 && retry) {
    const seconds = Number(response.headers.get("x-retry-after"));
    assert(Number.isInteger(seconds) && seconds > 0 && seconds <= 60);
    await response.body?.cancel();
    await new Promise((done) => setTimeout(done, seconds * 1000));
    return request(service, path, status, {
      bearer,
      method,
      body,
      retry: false,
      extraHeaders,
    });
  }
  assert.equal(response.status, status, `${method} ${path}`);
  return { data: await response.json(), headers: response.headers };
}
async function signup() {
  const response = await request("auth", "/api/auth/sign-up/email", 200, {
    method: "POST",
    body: {
      email: `chat-${randomUUID()}@example.invalid`,
      password: randomUUID() + randomUUID(),
      name: "Chat Contract",
    },
  });
  const session = response.headers.get("set-auth-token");
  assert(session);
  sessions.push(session);
  return (await request("auth", "/api/auth/token", 200, { bearer: session }))
    .data.token;
}
async function createSession(bearer, title, appId) {
  return (
    await request("api", "/v2/chat-sessions", 200, {
      bearer,
      method: "POST",
      body: { title, app_id: appId },
    })
  ).data.id;
}
const pass = (id) => report.cases.push({ id, result: "pass" });
try {
  const clientPath = resolve(metadata.trace_dir, "upstream-chat-client.mjs");
  const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
  const bundled = await build({
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
            path: "public-auth-token",
            namespace: "fixture",
          }));
          build.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({
            contents:
              "export const getIdToken = () => globalThis.__chatContractToken();",
            loader: "js",
          }));
        },
      },
    ],
  });
  writeFileSync(clientPath, bundled.outputFiles[0].contents, { mode: 0o600 });
  globalThis.__chatContractToken = async () => token;
  globalThis.fetch = async (input, init) => {
    assert.equal(typeof input, "string");
    assert(input.startsWith("/api/proxy/"), "unexpected client egress");
    const path = input.slice("/api/proxy".length);
    const response = await originalFetch(metadata.api_origin + path, {
      ...init,
      signal: AbortSignal.timeout(20000),
      redirect: "error",
    });
    trace.push({
      service: "upstream-client",
      method: init?.method ?? "GET",
      route: path
        .replace(/\/shared\/[^/?]+/g, "/shared/:token")
        .replace(/[0-9a-f]{8}-[0-9a-f-]{27}/g, ":id"),
      status: response.status,
    });
    return response;
  };
  const client = await import(pathToFileURL(clientPath));
  const owner = await signup(),
    other = await signup();
  const A = await createSession(owner, "Synthetic A"),
    B = await createSession(owner, "Synthetic B"),
    C = await createSession(other, "Synthetic C");
  token = owner;
  async function send(text, session, options = {}) {
    const chunks = [];
    await client.sendMessageStream(text, (chunk) => chunks.push(chunk), {
      chatSessionId: session,
      ...options,
    });
    assert.equal(
      chunks.filter((chunk) => chunk.type === "done").length,
      1,
      "SSE must complete exactly once",
    );
    return chunks;
  }
  const greeting = await client.getMessages(undefined, A);
  assert.equal(
    greeting[0].text,
    `Hi! I'm ${fixture.brand_runtime.ai_persona_name}. How can I help?`,
  );
  const brandReply = await send("[fixture:brand-prompt] Omi stays literal", A);
  const brandText = brandReply.find((chunk) => chunk.type === "done").message
    .text;
  assert(
    brandText.includes(
      `You are ${fixture.brand_runtime.ai_persona_name}, a concise`,
    ),
  );
  assert(brandText.includes("Omi stays literal"));
  assert(!brandText.includes("You are Omi,"));
  await client.clearMessages(undefined, A);
  pass("manifest-brand-greeting-and-model-prompt");
  const first = await send("Synthetic A question 中文", A);
  // Current parser returns the decoded ServerMessage as done.message.
  const done = first.find((chunk) => chunk.type === "done");
  assert.equal(done.message.chat_session_id, A);
  assert.equal(
    first
      .filter((chunk) => chunk.type === "data")
      .map((chunk) => chunk.text)
      .join(""),
    done.message.text,
  );
  assert(
    done.message.text.includes("回答\n") && done.message.text.includes("中文"),
  );
  const history = await client.getMessages(undefined, A);
  assert.equal(history.length, 2);
  assert(
    history.some(
      (row) => row.id === done.message.id && row.text === done.message.text,
    ),
  );
  assert(history.every((row) => row.chat_session_id === A));
  pass("upstream-web-sse-utf8-persisted-pair");
  const generated = (
    await request("api", "/v1/app/generate", 200, {
      bearer: owner,
      method: "POST",
      body: { prompt: "Build an Omi research assistant" },
    })
  ).data;
  assert.equal(generated.app.name, "Omi Research");
  assert(
    generated.app.description.startsWith(
      `You are an expert app designer for ${fixture.brand_runtime.display_name},`,
    ),
  );
  assert(generated.app.chat_prompt.endsWith("Build an Omi research assistant"));
  const goal = (
    await request("api", "/v1/goals", 200, {
      bearer: owner,
      method: "POST",
      body: {
        title: "Omi research goal",
        goal_type: "numeric",
        target_value: 3,
      },
    })
  ).data;
  const advice = (
    await request("api", `/v1/goals/${goal.id}/advice`, 200, { bearer: owner })
  ).data.advice;
  assert(advice.includes("GOAL: Omi research goal"));
  assert(
    advice.includes(`${fixture.brand_runtime.ai_persona_name}: Synthetic chat`),
  );
  await request("api", `/v1/goals/${goal.id}/advice`, 404, { bearer: other });
  pass("configured-app-platform-and-goal-chat-persona");
  const overage = (
    await request("api", "/v1/payments/overage-info", 200, { bearer: owner })
  ).data;
  assert(
    overage.explainer_body.includes(
      `${fixture.brand_runtime.display_name} is free`,
    ),
  );
  const task = (
    await request("api", "/v1/action-items", 200, {
      bearer: owner,
      method: "POST",
      body: { description: "Omi shared task stays literal" },
    })
  ).data;
  const share = (
    await request("api", "/v1/action-items/share", 200, {
      bearer: owner,
      method: "POST",
      body: { task_ids: [task.id] },
    })
  ).data;
  const preview = (
    await request("api", `/v1/action-items/shared/${share.token}`, 200)
  ).data;
  assert.equal(
    preview.sender_name,
    `${fixture.brand_runtime.display_name} user`,
  );
  assert.equal(preview.tasks[0].description, "Omi shared task stays literal");
  const accepted = (
    await request("api", "/v1/action-items/accept", 200, {
      bearer: other,
      method: "POST",
      body: { token: share.token },
    })
  ).data;
  const copied = (
    await request("api", `/v1/action-items/${accepted.created[0]}`, 200, {
      bearer: other,
    })
  ).data;
  assert.equal(copied.shared_from.sender_name, preview.sender_name);
  assert.equal(copied.description, "Omi shared task stays literal");
  // Read only this fixture's private generated admin credential. HTTP traces
  // record route/status only; no credential is exported or printed.
  const adminVars = readFileSync(
    resolve(dirname(values.metadata), "configs/api-core/.dev.vars"),
    "utf8",
  );
  const adminKey = fixtureSecret(adminVars, "FAIR_USE_ADMIN_KEY");
  const ownerUid = JSON.parse(
    Buffer.from(owner.split(".")[1], "base64url"),
  ).sub;
  assert.equal(typeof ownerUid, "string");
  const adminPath = `/v1/admin/fair-use/user/${encodeURIComponent(ownerUid)}`;
  try {
    for (const stage of ["warning", "restrict"]) {
      await request("api", `${adminPath}/set-stage?stage=${stage}`, 200, {
        bearer: owner,
        method: "POST",
        extraHeaders: { "x-admin-key": adminKey },
      });
      const status = (
        await request("api", "/v1/fair-use/status", 200, { bearer: owner })
      ).data;
      assert.equal(status.stage, stage);
      assert(
        status.message.includes(
          stage === "warning"
            ? `${fixture.brand_runtime.display_name} is designed`
            : fixture.support_email,
        ),
      );
    }
  } finally {
    await request("api", `${adminPath}/reset`, 200, {
      bearer: owner,
      method: "POST",
      extraHeaders: { "x-admin-key": adminKey },
    });
  }
  pass("configured-usage-support-and-task-share-presentation");
  await send("Synthetic B private question", B);
  const again = await send("Synthetic A follow-up", A, {
    appId: "must-not-override-selected-app",
  });
  const answer = again.find((chunk) => chunk.type === "done").message.text;
  assert(
    answer.includes("Synthetic A question") &&
      !answer.includes("Synthetic B private"),
  );
  assert.equal((await client.getMessages(undefined, B)).length, 2);
  pass("selected-history-and-app-authority");
  token = other;
  await send("Synthetic C private question", C);
  for (const method of ["GET", "POST", "DELETE"]) {
    const result = await request(
      "api",
      `/v2/messages?chat_session_id=${A}`,
      404,
      {
        bearer: other,
        method,
        body: method === "POST" ? { text: "foreign" } : undefined,
      },
    );
    assert.deepEqual(result.data, { detail: "Chat session not found" });
  }
  assert.equal((await client.getMessages(undefined, C)).length, 2);
  pass("cross-uid-read-write-clear-denied");
  token = owner;
  const coach = await createSession(owner, "Synthetic Coach", "fixture-coach");
  await request("api", "/v2/desktop/messages", 200, {
    bearer: owner,
    method: "POST",
    body: {
      session_id: coach,
      app_id: "fixture-coach",
      sender: "human",
      text: "Synthetic coach context",
    },
  });
  const retryKey = randomUUID();
  const complete = (session, expected) =>
    request("api", "/v2/cf/chat/completions", expected, {
      bearer: owner,
      method: "POST",
      extraHeaders: { "Idempotency-Key": retryKey },
      body: {
        session_id: session,
        messages: [{ role: "user", content: "Synthetic follow-up" }],
      },
    });
  const completion = await complete(coach, 200);
  assert(
    completion.data.choices[0].message.content.includes(
      "Synthetic coach context",
    ),
  );
  assert.equal((await complete(coach, 200)).data.id, completion.data.id);
  await complete(B, 409);
  await complete(C, 404);
  await complete(randomUUID(), 404);
  assert.equal((await client.getMessages(undefined, B)).length, 2);
  pass("compat-replay-and-context-respect-selected-target");
  const before = await client.getMessages(undefined, A);
  await assert.rejects(
    () =>
      client.sendMessageStream("[fixture:provider-error]", () => {}, {
        chatSessionId: A,
      }),
    /502/,
  );
  assert.deepEqual(await client.getMessages(undefined, A), before);
  pass("actual-model-rpc-failure-no-partial-history");
  await client.clearMessages(undefined, A);
  const selected = await request("api", `/v2/chat-sessions/${A}`, 200, {
    bearer: owner,
  });
  assert.equal(selected.data.message_count, 0);
  assert.equal((await client.getMessages(undefined, B)).length, 2);
  const emptyPage = await request(
    "api",
    `/v2/messages?chat_session_id=${A}&offset=1`,
    200,
    { bearer: owner },
  );
  assert.deepEqual(emptyPage.data, []);
  await send("Synthetic A after clear", A);
  assert.equal((await client.getMessages(undefined, A)).length, 2);
  await request("api", `/v2/chat-sessions/${A}`, 200, {
    bearer: owner,
    method: "DELETE",
  });
  await request("api", `/v2/messages?chat_session_id=${A}`, 404, {
    bearer: owner,
    method: "POST",
    body: { text: "deleted" },
  });
  pass("explicit-clear-preserves-session-delete-stays-terminal");
  await request("api", `/v2/chat-sessions/${B}`, 200, {
    bearer: owner,
    method: "DELETE",
  });
  const defaultSessions = async () =>
    (await request("api", "/v2/chat-sessions", 200, { bearer: owner })).data;
  async function control(path, method = "GET") {
    const response = await originalFetch(controlOrigin + path, {
      method,
      signal: AbortSignal.timeout(20000),
      redirect: "error",
    });
    assert.equal(response.status, 200, `inference control ${path}`);
    return response;
  }
  for (const mutation of [
    "default-clear",
    "session-delete",
    "explicit-clear",
  ]) {
    assert.equal((await defaultSessions()).length, 0);
    const { id } = await (await control("/gates", "POST")).json();
    // Attach both handlers immediately; the model can finish only after release.
    const pending = send(`[fixture:wait:${id}]`).then(
      () => null,
      (error) => error,
    );
    let selectedId,
      started = false;
    try {
      await control(`/started/${id}`);
      started = true;
      const visible = await defaultSessions();
      assert.equal(
        visible.length,
        1,
        "first model IO must own a visible session",
      );
      selectedId = visible[0].id;
      assert.equal(visible[0].message_count, 0);
      if (mutation === "session-delete")
        await request("api", `/v2/chat-sessions/${selectedId}`, 200, {
          bearer: owner,
          method: "DELETE",
        });
      else
        await client.clearMessages(
          undefined,
          mutation === "explicit-clear" ? selectedId : undefined,
        );
    } finally {
      if (started) await control(`/release/${id}`, "POST");
    }
    assert.match((await pending)?.message ?? "unexpected success", /503/);
    if (mutation === "explicit-clear") {
      const retained = await defaultSessions();
      assert.equal(retained.length, 1);
      assert.equal(retained[0].id, selectedId);
      assert.equal(retained[0].message_count, 0);
      await client.clearMessages();
    }
    assert.equal((await defaultSessions()).length, 0);
    await request("api", `/v2/messages?chat_session_id=${selectedId}`, 404, {
      bearer: owner,
    });
  }
  pass("first-model-io-visible-owner-clear-delete-reject-late-result");
  await assert.rejects(() => send("[fixture:provider-error]"), /502/);
  const failed = await defaultSessions();
  assert.equal(failed.length, 1);
  assert.equal(failed[0].message_count, 0);
  await client.clearMessages();
  assert.equal((await defaultSessions()).length, 0);
  pass("failed-first-model-leaves-an-empty-publicly-clearable-session");
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.cases.push({
    id: "execution",
    result: "fail",
    message: error.message,
  });
  process.exitCode = 1;
} finally {
  globalThis.fetch = originalFetch;
  delete globalThis.__chatContractToken;
  for (const bearer of sessions) {
    try {
      await request("auth", "/api/auth/sign-out", 200, {
        bearer,
        method: "POST",
        body: {},
      });
    } catch {
      report.passed = false;
      process.exitCode = 1;
    }
  }
  writeFileSync(
    resolve(metadata.trace_dir, "chat-results.json"),
    JSON.stringify(report, null, 2) + "\n",
    { mode: 0o600 },
  );
  writeFileSync(
    resolve(metadata.trace_dir, "chat-http.jsonl"),
    trace.map((row) => JSON.stringify(row)).join("\n") + "\n",
    { mode: 0o600 },
  );
  console.log(JSON.stringify(report));
}
