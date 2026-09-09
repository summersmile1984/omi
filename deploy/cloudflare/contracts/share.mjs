import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({ options: { metadata: { type: "string" } } });
if (!values.metadata) throw new Error("--metadata is required");
const metadata = JSON.parse(readFileSync(values.metadata));
const fixture = JSON.parse(
  readFileSync(resolve(dirname(values.metadata), "fixture.json")),
);
const shareOrigin = fixture.public_share_origin;
const senderName = `${fixture.brand_runtime.display_name} user`;
assert.equal(fixture.brand_runtime.brand_id, metadata.brand_id);
for (const value of [
  metadata.api_origin,
  metadata.auth_origin,
  shareOrigin,
  ...(metadata.web_origin ? [metadata.web_origin] : []),
]) {
  const url = new URL(value);
  assert(
    url.protocol === "http:" &&
      ["127.0.0.1", "localhost"].includes(url.hostname) &&
      url.port &&
      url.pathname === "/" &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash,
    "share contract requires explicit loopback origins",
  );
}
mkdirSync(metadata.trace_dir, { recursive: true, mode: 0o700 });
const report = {
  schema_version: 1,
  scope: "public-chat-task-share",
  target: metadata.target,
  brand_id: metadata.brand_id,
  cases: [],
  release_qualified: false,
};
const trace = [],
  sessions = [];
const scrub = (path) =>
  path
    .replace(/\/shared\/[a-f0-9]{32}/gi, "/shared/:token")
    .replace(/\/(?:chat|tasks)\/[a-f0-9]{32}/gi, "/:kind/:token")
    .replace(/[0-9a-f]{8}-[0-9a-f-]{27}/gi, ":id");
async function request(
  service,
  path,
  status,
  { bearer, body, method = "GET", retry = true } = {},
) {
  const response = await fetch(metadata[`${service}_origin`] + path, {
    method,
    redirect: "error",
    signal: AbortSignal.timeout(20000),
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      Origin: shareOrigin,
      "X-App-Platform": "web",
      ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  trace.push({ service, method, route: scrub(path), status: response.status });
  if (path === "/api/auth/sign-up/email" && response.status === 429 && retry) {
    const seconds = Number(response.headers.get("x-retry-after"));
    assert(Number.isInteger(seconds) && seconds > 0 && seconds <= 60);
    await response.body?.cancel();
    await new Promise((done) => setTimeout(done, seconds * 1000));
    return request(service, path, status, {
      bearer,
      body,
      method,
      retry: false,
    });
  }
  assert.equal(response.status, status, `${method} ${scrub(path)}`);
  return { body: await response.json(), headers: response.headers };
}
async function signup(label) {
  const result = await request("auth", "/api/auth/sign-up/email", 200, {
    method: "POST",
    body: {
      email: `share-${randomUUID()}@example.invalid`,
      password: randomUUID() + randomUUID(),
      name: label,
    },
  });
  const session = result.headers.get("set-auth-token");
  assert(session);
  sessions.push(session);
  return (await request("auth", "/api/auth/token", 200, { bearer: session }))
    .body.token;
}
const pass = (id) => report.cases.push({ id, result: "pass" });
try {
  if (metadata.web_origin) {
    for (const path of ["favicon.png", "logo.png"]) {
      const response = await fetch(`${metadata.web_origin}/${path}`, {
        redirect: "error",
        signal: AbortSignal.timeout(10000),
      });
      trace.push({
        service: "web",
        method: "GET",
        route: `/${path}`,
        status: response.status,
      });
      assert.equal(response.status, 200, `frozen Web asset ${path}`);
      const hash = createHash("sha256")
        .update(Buffer.from(await response.arrayBuffer()))
        .digest("hex");
      assert.equal(
        hash,
        fixture.artifacts.web.assets[path],
        `frozen Web asset bytes ${path}`,
      );
    }
    pass("frozen-web-static-assets-served-with-exact-bytes");
  }
  const owner = await signup("Share Owner"),
    recipient = await signup("Share Recipient");
  const task = (
    await request("api", "/v1/action-items", 200, {
      bearer: owner,
      method: "POST",
      body: { description: "Synthetic private share task" },
    })
  ).body;
  await request("api", "/v1/action-items/share", 404, {
    bearer: recipient,
    method: "POST",
    body: { task_ids: [task.id] },
  });
  const taskShare = (
    await request("api", "/v1/action-items/share", 200, {
      bearer: owner,
      method: "POST",
      body: { task_ids: [task.id] },
    })
  ).body;
  assert.equal(taskShare.url, `${shareOrigin}/tasks/${taskShare.token}`);
  const taskPreview = (
    await request("api", `/v1/action-items/shared/${taskShare.token}`, 200)
  ).body;
  assert.deepEqual(taskPreview, {
    sender_name: senderName,
    tasks: [{ description: "Synthetic private share task", due_at: null }],
    count: 1,
  });
  if (metadata.web_origin) {
    const preview = await request(
      "web",
      `/api/proxy/public/v1/action-items/shared/${taskShare.token}`,
      200,
    );
    assert.deepEqual(preview.body, taskPreview);
    assert.equal(preview.headers.get("cache-control"), "private, no-store");
    const missing = await request(
      "web",
      `/api/proxy/public/v1/action-items/shared/${"0".repeat(32)}`,
      404,
    );
    assert.equal(missing.headers.get("cache-control"), "private, no-store");
    assert.deepEqual(missing.body, { error: "Share unavailable" });
    pass("frozen-web-task-share-service-binding-and-missing-link");
  }
  await request("api", "/v1/action-items/accept", 400, {
    bearer: owner,
    method: "POST",
    body: { token: taskShare.token },
  });
  const accepted = (
    await request("api", "/v1/action-items/accept", 200, {
      bearer: recipient,
      method: "POST",
      body: { token: taskShare.token },
    })
  ).body;
  assert.equal(accepted.count, 1);
  assert.equal(
    (
      await request("api", `/v1/action-items/${accepted.created[0]}`, 200, {
        bearer: recipient,
      })
    ).body.description,
    "Synthetic private share task",
  );
  await request("api", "/v1/action-items/accept", 409, {
    bearer: recipient,
    method: "POST",
    body: { token: taskShare.token },
  });
  await request("api", "/v1/action-items/accept", 422, {
    bearer: recipient,
    method: "POST",
    body: {},
  });
  await request("api", `/v1/action-items/shared/${"0".repeat(32)}`, 404);
  pass("task-preview-owner-recipient-and-idempotent-acceptance");

  const session = (
    await request("api", "/v2/chat-sessions", 200, {
      bearer: owner,
      method: "POST",
      body: { title: "Shared chat" },
    })
  ).body.id;
  const message = (
    await request("api", "/v2/desktop/messages", 200, {
      bearer: owner,
      method: "POST",
      body: {
        session_id: session,
        sender: "human",
        text: "Synthetic private chat selection",
      },
    })
  ).body;
  await request("api", "/v2/messages/share", 404, {
    bearer: recipient,
    method: "POST",
    body: { message_ids: [message.id] },
  });
  const chatShare = (
    await request("api", "/v2/messages/share", 200, {
      bearer: owner,
      method: "POST",
      body: { message_ids: [message.id] },
    })
  ).body;
  assert.equal(chatShare.url, `${shareOrigin}/chat/${chatShare.token}`);
  const chatPreview = (
    await request("api", `/v2/messages/shared/${chatShare.token}`, 200)
  ).body;
  assert.equal(chatPreview.sender_name, senderName);
  assert.equal(chatPreview.count, 1);
  assert.equal(
    chatPreview.messages[0].text,
    "Synthetic private chat selection",
  );
  assert.equal(chatPreview.messages[0].id, message.id);
  if (metadata.web_origin) {
    const preview = await request(
      "web",
      `/api/proxy/public/v2/messages/shared/${chatShare.token}`,
      200,
    );
    assert.deepEqual(preview.body, {
      sender_name: senderName,
      count: 1,
      messages: [
        {
          text: "Synthetic private chat selection",
          sender: chatPreview.messages[0].sender,
          created_at: chatPreview.messages[0].created_at,
        },
      ],
    });
    assert.equal(preview.headers.get("cache-control"), "private, no-store");
    await request(
      "web",
      `/api/proxy/public/v2/messages/shared/${"0".repeat(32)}`,
      404,
    );
    pass("frozen-web-selected-chat-share-service-binding-and-missing-link");
  }
  await request("api", `/v2/messages/shared/${"0".repeat(32)}`, 404);
  pass("chat-preview-only-exposes-selected-owner-message");
  report.passed = true;
} finally {
  for (const session of sessions)
    try {
      await request("auth", "/api/auth/sign-out", 200, {
        bearer: session,
        method: "POST",
        body: {},
      });
    } catch {}
  writeFileSync(
    resolve(metadata.trace_dir, "share-http.jsonl"),
    trace.map((entry) => JSON.stringify(entry)).join("\n") + "\n",
    { mode: 0o600 },
  );
  writeFileSync(
    resolve(metadata.trace_dir, "share-results.json"),
    JSON.stringify(report, null, 2) + "\n",
    { mode: 0o600 },
  );
}
if (!report.passed) process.exitCode = 1;
