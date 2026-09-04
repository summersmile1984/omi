import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import { WebSocket } from "ws";

// Actual public recording flow; only the target runner's inference is controlled.
// Kept separate from the common core slice until both target runners support it.
const { values } = parseArgs({ options: { metadata: { type: "string" } } });
const metadata = JSON.parse(readFileSync(values.metadata));
for (const key of ["api_origin", "auth_origin"]) {
  const url = new URL(metadata[key]);
  if (
    url.protocol !== "http:" ||
    !["127.0.0.1", "localhost"].includes(url.hostname) ||
    !url.port ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  )
    throw new Error("recording contract requires an explicit loopback target");
}
const report = {
  schema_version: 1,
  scope: "recording-persistence-finalization",
  target: metadata.target,
  brand_id: metadata.brand_id,
  cases: [],
  release_qualified: false,
};
const sessions = [],
  sockets = [],
  trace = [];
const require = (value, message) => {
  if (!value) throw new Error(message);
};
async function request(
  service,
  path,
  status,
  { token, method = "GET", body, retrySignup = true } = {},
) {
  const response = await fetch(metadata[`${service}_origin`] + path, {
    method,
    redirect: "error",
    signal: AbortSignal.timeout(20000),
    headers: {
      Origin: metadata.auth_origin,
      "Content-Type": "application/json",
      "X-App-Platform": "web",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  trace.push({
    service,
    method,
    route: path.replace(/[0-9a-f]{8}-[0-9a-f-]{27}/g, ":id"),
    status: response.status,
    expected: status,
  });
  if (
    service === "auth" &&
    path === "/api/auth/sign-up/email" &&
    response.status === 429 &&
    retrySignup
  ) {
    const retryAfter = Number(response.headers.get("x-retry-after"));
    require(Number.isInteger(retryAfter) &&
      retryAfter > 0 &&
      retryAfter <= 60, "signup rate-limit omitted a bounded retry interval");
    await response.body?.cancel();
    await new Promise((resolve) => setTimeout(resolve, retryAfter * 1000));
    return request(service, path, status, {
      token,
      method,
      body,
      retrySignup: false,
    });
  }
  require(response.status ===
    status, `${service} ${method} expected ${status}, received ${response.status}`);
  const data = await response.json();
  return { data, headers: response.headers };
}
async function signup() {
  const result = await request("auth", "/api/auth/sign-up/email", 200, {
    method: "POST",
    body: {
      email: `record-${randomUUID()}@example.invalid`,
      password: randomUUID() + randomUUID(),
      name: "Recording Contract",
    },
  });
  const session = result.headers.get("set-auth-token");
  require(session, "signup omitted opaque session");
  sessions.push(session);
  const jwt = await request("auth", "/api/auth/token", 200, { token: session });
  return { token: jwt.data.token, session };
}
async function connect(token, id, native = false) {
  const socket = new WebSocket(
    metadata.api_origin.replace("http:", "ws:") +
      `${
        native ? "/v4/listen" : "/v4/web/listen"
      }?codec=pcm16&sample_rate=16000&language=en&client_conversation_id=${id}`,
    native ? { headers: { Authorization: `Bearer ${token}` } } : {},
  );
  sockets.push(socket);
  const frames = [];
  const listeners = new Set();
  let closed = false;
  const notify = () => {
    for (const listener of listeners) listener();
  };
  socket.on("message", (data) => {
    frames.push(JSON.parse(data.toString()));
    notify();
  });
  socket.on("close", () => {
    closed = true;
    notify();
  });
  socket.on("error", notify);
  const until = (predicate) =>
    new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => done(new Error("recording frame deadline exceeded")),
        15000,
      );
      const done = (error) => {
        clearTimeout(timer);
        listeners.delete(check);
        error ? reject(error) : resolve();
      };
      const check = () =>
        predicate(frames)
          ? done()
          : closed
          ? done(new Error("recording closed before expected event"))
          : undefined;
      listeners.add(check);
      check();
    });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error("recording upgrade deadline exceeded")),
      15000,
    );
    socket.once("open", () => {
      clearTimeout(timer);
      resolve();
    });
    socket.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
  });
  if (!native) socket.send(JSON.stringify({ type: "auth", token }));
  await until((frames) =>
    frames.some((frame) => frame.type === "conversation_session"),
  );
  return {
    socket,
    frames,
    until,
    event: frames.find((frame) => frame.type === "conversation_session"),
  };
}
async function caseOf(id, action) {
  await action();
  report.cases.push({ id, result: "pass" });
}
try {
  const owner = await signup(),
    other = await signup(),
    id = randomUUID();
  await caseOf("recording.native-upgrade-bearer-pcm-persistence", async () => {
    const nativeId = randomUUID();
    const native = await connect(owner.token, nativeId, true);
    native.socket.send(new Uint8Array(16000));
    await native.until((frames) => frames.some(Array.isArray));
    const stored = await request("api", `/v1/conversations/${nativeId}`, 200, {
      token: owner.token,
    });
    require(stored.data.transcript_segments.length ===
      1, "native displayed transcript was not persisted");
    native.socket.close();
    await new Promise((resolve) => native.socket.once("close", resolve));
  });
  const first = await connect(owner.token, id);
  await caseOf("recording.auth-session-commit-before-broadcast", async () => {
    require(first.event.conversation_id === id &&
      first.event.lifecycle_phase ===
        "in_progress", "recording identity differs");
    first.socket.send(new Uint8Array(16000));
    await first.until((frames) => frames.some(Array.isArray));
    const stored = await request("api", `/v1/conversations/${id}`, 200, {
      token: owner.token,
    });
    require(stored.data.status === "in_progress" &&
      stored.data.transcript_segments.length ===
        1, "displayed transcript was not persisted");
    await request("api", `/v1/conversations/${id}`, 404, {
      token: other.token,
    });
    await request("api", `/v1/conversations/${id}/finalize`, 404, {
      token: other.token,
      method: "POST",
      body: {},
    });
  });
  const resumed = await connect(owner.token, id);
  await caseOf("recording.reconnect-fences-stale-writer", async () => {
    require(resumed.event.conversation_id === id &&
      resumed.event.lifecycle_sequence >
        first.event.lifecycle_sequence, "resume lost identity or generation");
    first.socket.send(new Uint8Array(16000));
    await first.until((frames) =>
      frames.some(
        (frame) =>
          frame.type === "recording_unavailable" ||
          frame.error === "recording_unavailable",
      ),
    );
    require(first.frames.filter(Array.isArray).length ===
      1, "stale writer displayed uncommitted transcript");
    resumed.socket.send(new Uint8Array(16000));
    await resumed.until((frames) => frames.some(Array.isArray));
    const stored = await request("api", `/v1/conversations/${id}`, 200, {
      token: owner.token,
    });
    require(stored.data.transcript_segments.length === 2 &&
      stored.data.transcript_segments[1].start ===
        1, "resume overwrote prior transcript");
    resumed.socket.close();
    await new Promise((resolve) => resumed.socket.once("close", resolve));
  });
  await caseOf(
    "recording.actual-queue-finalization-memory-and-tasks",
    async () => {
      await request("api", `/v1/conversations/${id}/finalize`, 200, {
        token: owner.token,
        method: "POST",
        body: {},
      });
      let completed = false;
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        const stored = await request("api", `/v1/conversations/${id}`, 200, {
          token: owner.token,
        });
        if (stored.data.status === "completed") {
          completed = true;
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      require(completed, "actual queue did not finalize recording");
      const memories = await request("api", "/v3/memories", 200, {
        token: owner.token,
      });
      const tasks = await request("api", "/v1/action-items", 200, {
        token: owner.token,
      });
      require(JSON.stringify(memories.data).includes(
        "prefers concise updates",
      ), "recording memory was not derived");
      require(JSON.stringify(tasks.data).includes(
        "Send the synthetic follow-up",
      ), "recording task was not derived");
      for (const path of ["/v3/memories", "/v1/action-items"]) {
        const isolated = await request("api", path, 200, {
          token: other.token,
        });
        require(!JSON.stringify(isolated.data).includes("concise updates") &&
          !JSON.stringify(isolated.data).includes(
            "synthetic follow-up",
          ), "derived state crossed account boundary");
      }
    },
  );
  await caseOf("recording.completed-generation-never-reopened", async () => {
    const next = await connect(owner.token, id);
    require(next.event.conversation_id !==
      id, "completed recording was reopened");
    next.socket.close();
    const stored = await request("api", `/v1/conversations/${id}`, 200, {
      token: owner.token,
    });
    require(stored.data.status === "completed", "prior generation changed");
  });
  await caseOf("recording.revoked-session-denied", async () => {
    await request("auth", "/api/auth/sign-out", 200, {
      token: owner.session,
      method: "POST",
      body: {},
    });
    await request("api", `/v1/conversations/${id}`, 401, {
      token: owner.token,
    });
  });
  report.passed = true;
} catch (error) {
  report.passed = false;
  // Only local assertion messages, never provider bodies or credentials.
  report.cases.push({
    id: "recording-flow",
    result: "fail",
    error: error.message,
  });
  process.exitCode = 1;
} finally {
  for (const socket of sockets) socket.terminate();
  for (const session of sessions)
    try {
      await request("auth", "/api/auth/sign-out", 200, {
        token: session,
        method: "POST",
        body: {},
      });
    } catch {}
  mkdirSync(metadata.trace_dir, { recursive: true, mode: 0o700 });
  writeFileSync(
    resolve(metadata.trace_dir, "recording-results.json"),
    JSON.stringify(report, null, 2) + "\n",
    { mode: 0o600 },
  );
  writeFileSync(
    resolve(metadata.trace_dir, "recording-http.jsonl"),
    trace.map((row) => JSON.stringify(row)).join("\n") + "\n",
    { mode: 0o600 },
  );
  console.log(JSON.stringify(report));
}
