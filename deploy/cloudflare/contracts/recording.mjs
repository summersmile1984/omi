import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import { WebSocket } from "ws";
import { localPrivacyObserver } from "./local-privacy.mjs";

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
  scope: "recording-persistence-finalization-and-privacy",
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
  {
    token,
    method = "GET",
    body,
    retrySignup = true,
    rawBody,
    bytes = false,
    headers = {},
  } = {},
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
      ...headers,
    },
    body: rawBody ?? (body === undefined ? undefined : JSON.stringify(body)),
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
  require((Array.isArray(status) ? status : [status]).includes(
    response.status,
  ), `${service} ${method} expected ${status}, received ${response.status}`);
  const data = bytes
    ? Buffer.from(await response.arrayBuffer())
    : await response.json();
  return { data, headers: response.headers, status: response.status };
}
async function signup() {
  const email = `record-${randomUUID()}@example.invalid`,
    password = randomUUID() + randomUUID();
  const result = await request("auth", "/api/auth/sign-up/email", 200, {
    method: "POST",
    body: {
      email,
      password,
      name: "Recording Contract",
    },
  });
  const session = result.headers.get("set-auth-token");
  require(session, "signup omitted opaque session");
  sessions.push(session);
  const jwt = await request("auth", "/api/auth/token", 200, { token: session });
  return {
    token: jwt.data.token,
    session,
    email,
    password,
    uid: JSON.parse(Buffer.from(jwt.data.token.split(".")[1], "base64url")).sub,
  };
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
  const restored = await request("auth", "/api/auth/sign-in/email", 200, {
    method: "POST",
    body: { email: owner.email, password: owner.password },
  });
  owner.session = restored.headers.get("set-auth-token");
  require(owner.session, "sign-in omitted restored session");
  sessions.push(owner.session);
  owner.token = (
    await request("auth", "/api/auth/token", 200, { token: owner.session })
  ).data.token;
  await caseOf("recording.desktop-daily-usage-concurrent-maxima", async () => {
    const data = {
      date: new Date().toISOString().slice(0, 10),
      timezone: "UTC",
      client_device_id: "contract-desktop",
      watching_seconds: 120,
      listening_seconds: 80,
      proactive_cards_shown: 3,
      proactive_cards_acted: 1,
      ptt_turns: 2,
    };
    await request("api", "/v1/users/desktop-usage/daily", 401, {
      method: "POST",
      body: data,
    });
    await request("api", "/v1/users/desktop-usage/daily", 422, {
      token: owner.token,
      method: "POST",
      body: { ...data, ptt_turns: true },
    });
    await Promise.all(
      [120, 40, 60, 90].map((watching_seconds) =>
        request("api", "/v1/users/desktop-usage/daily", 200, {
          token: owner.token,
          method: "POST",
          body: { ...data, watching_seconds },
        }),
      ),
    );
    const exported = await request("api", "/v1/users/export", 200, {
      token: owner.token,
    });
    require(exported.data.desktop_daily_usage.length === 1 &&
      exported.data.desktop_daily_usage[0].watching_seconds ===
        120, "concurrent desktop counter reports lost a running maximum");
  });
  await caseOf("recording.daily-recap-from-persisted-recording", async () => {
    const options = {
      token: owner.token,
      method: "POST",
      body: { date: new Date().toISOString().slice(0, 10) },
    };
    const created = await request(
      "api",
      "/v1/users/daily-summaries",
      200,
      options,
    );
    require(created.data.headline === "Synthetic daily recap" &&
      created.data.overview.includes(
        "provider-controlled recording",
      ), "recap did not use model with persisted recording context");
    require(created.data.highlights.some((row) =>
      row.conversation_ids.includes(id),
    ), "recap citation omitted the recorded source");
    require(created.data.action_items.some(
      (row) =>
        row.description === "Send the synthetic follow-up" &&
        row.source_conversation_id === id,
    ), "recap omitted the persisted task");
    require(created.data.memories_learned.some(
      (row) => row.memory_id && row.content.includes("prefers concise updates"),
    ), "recap omitted a canonical learned memory");
    require(created.data.stats.watching_minutes === 2 &&
      created.data.stats.proactive_moments ===
        3, "recap did not join device counters");
    const exported = await request("api", "/v1/users/export", 200, {
      token: owner.token,
    });
    const recap = exported.data.daily_summaries.find(
      (row) => row.id === created.data.id,
    );
    require(recap?.headline === created.data.headline &&
      !Object.hasOwn(
        recap,
        "generation_token",
      ), "export omitted recap or exposed generation authority");
    const reused = await request(
      "api",
      "/v1/users/daily-summaries",
      200,
      options,
    );
    require(JSON.stringify(reused.data) ===
      JSON.stringify(created.data), "daily create was not idempotent");
    await request("api", `/v1/users/daily-summaries/${created.data.id}`, 404, {
      token: other.token,
    });
    const usage = await request("api", "/v1/users/me/llm-usage", 200, {
      token: owner.token,
    });
    require(usage.data.summary.daily_summary?.call_count ===
      1, "daily recap reuse spent a second model call");
    const regenerated = await request(
      "api",
      `/v1/users/daily-summaries/${created.data.id}/regenerate`,
      200,
      { token: owner.token, method: "POST", body: {} },
    );
    require(regenerated.data.id === created.data.id &&
      regenerated.data.created_at === created.data.created_at &&
      regenerated.data
        .regenerated_at, "regeneration replaced recap identity or creation timestamp");
    await request(
      "api",
      `/v1/users/daily-summaries/${created.data.id}/regenerate`,
      429,
      { token: owner.token, method: "POST", body: {} },
    );
  });
  await caseOf(
    "recording.export-restored-recording-memory-task-and-profile",
    async () => {
      await request("api", "/v1/users/export", 401);
      const exported = await request("api", "/v1/users/export", 200, {
        token: owner.token,
      });
      require(exported.headers.get("content-disposition") ===
        `attachment; filename="${metadata.brand_id}-export.json"`, "export filename ignored the configured brand");
      require(exported.data.profile.uid === owner.uid &&
        exported.data.profile.email === owner.email &&
        exported.data.profile.name ===
          "Recording Contract", "export omitted authoritative profile");
      const conversation = exported.data.conversations.find(
        (row) => row.id === id,
      );
      require(conversation?.transcript_segments.length === 2 &&
        conversation.status === "completed", "export lost completed recording");
      require(JSON.stringify(exported.data.memories).includes(
        "prefers concise updates",
      ), "export lost derived memory");
      require(JSON.stringify(exported.data.action_items).includes(
        "Send the synthetic follow-up",
      ), "export lost derived task");
      const isolated = await request("api", "/v1/users/export", 200, {
        token: other.token,
      });
      require(!JSON.stringify(isolated.data).includes(id) &&
        !JSON.stringify(isolated.data).includes(
          owner.email,
        ), "export crossed account boundary");
    },
  );
  const assetPath = `/v1/cf/assets/privacy-${randomUUID()}.txt`,
    ownerBytes = Buffer.from("Owner synthetic private attachment"),
    otherBytes = Buffer.from("Other synthetic private attachment");
  const sha256 = (data) => createHash("sha256").update(data).digest("hex");
  await caseOf(
    "recording.private-r2-integrity-and-account-isolation",
    async () => {
      await request("api", assetPath, 422, {
        token: owner.token,
        method: "PUT",
        rawBody: ownerBytes,
        headers: {
          "Content-Type": "text/plain",
          "x-content-sha256": "0".repeat(64),
        },
      });
      const stored = await request("api", assetPath, 200, {
        token: owner.token,
        method: "PUT",
        rawBody: ownerBytes,
        headers: {
          "Content-Type": "text/plain",
          "x-content-sha256": sha256(ownerBytes),
        },
      });
      require(stored.data.checksum_sha256 ===
        sha256(ownerBytes), "R2 upload checksum differs");
      await request("api", assetPath, 404, { token: other.token });
      const read = await request("api", assetPath, 200, {
        token: owner.token,
        bytes: true,
      });
      require(read.data.equals(ownerBytes), "R2 attachment bytes differ");
      await request("api", assetPath, 200, {
        token: other.token,
        method: "PUT",
        rawBody: otherBytes,
        headers: { "Content-Type": "text/plain" },
      });
    },
  );
  const unused = await signup();
  const inspectPrivacy = await localPrivacyObserver(metadata);
  const beforeDeletion = inspectPrivacy(owner.uid);
  require(beforeDeletion.app["cf_conversations.uid"] > 0 &&
    beforeDeletion.app["cf_memories.uid"] > 0 &&
    beforeDeletion.app["cf_action_items.uid"] > 0 &&
    beforeDeletion.auth["user.id"] === 1 &&
    beforeDeletion.r2_objects >
      0, "privacy fixture did not persist real App/Auth/R2 data");
  await caseOf(
    "recording.account-deletion-initializes-unused-native-account",
    async () => {
      await request("api", "/v1/users/delete-account", 401, {
        method: "DELETE",
        body: {},
      });
      await request("api", "/v1/users/delete-account", 200, {
        token: unused.token,
        method: "DELETE",
        body: {},
      });
    },
  );
  await caseOf(
    "recording.account-deletion-fences-and-completes-from-queue",
    async () => {
      await request("api", "/v1/users/delete-account", 200, {
        token: owner.token,
        method: "DELETE",
        body: {},
      });
      await request("api", "/v1/users/export", 409, { token: owner.token });
      // Preserve production quiescence/settling delays. The real Queue consumer,
      // rather than a test-invoked processor or manual SQL, must revoke identity.
      const deadline = Date.now() + 180000;
      let revoked = false;
      while (Date.now() < deadline) {
        const check = await request(
          "api",
          `/v1/conversations/${id}`,
          [409, 401],
          { token: owner.token },
        );
        if (check.status === 401) {
          revoked = true;
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
      require(revoked, "account deletion queue did not revoke identity");
      await request("auth", "/api/auth/sign-in/email", 401, {
        method: "POST",
        body: { email: owner.email, password: owner.password },
      });
      const survivor = await request("api", assetPath, 200, {
        token: other.token,
        bytes: true,
      });
      require(survivor.data.equals(
        otherBytes,
      ), "account deletion removed another account's attachment");
      await request("api", "/v1/users/export", 200, { token: other.token });
      mkdirSync(metadata.trace_dir, { recursive: true, mode: 0o700 });
    },
  );
  await caseOf(
    "recording.account-deletion-zero-persisted-residual",
    async () => {
      const deadline = Date.now() + 15000;
      let erased;
      while (Date.now() < deadline) {
        erased = [owner, unused].map((account) => inspectPrivacy(account.uid));
        if (
          erased.every(
            (data) => data.app_tombstones === 1 && data.pending_deletions === 0,
          )
        )
          break;
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      for (const data of erased) {
        require(Object.values(data.app).every((count) => count === 0) &&
          Object.values(data.auth).every((count) => count === 0) &&
          data.r2_objects === 0 &&
          data.app_tombstones === 1 &&
          data.pending_deletions ===
            0, "account deletion left persisted App/Auth/R2 residual");
      }
      const survivor = inspectPrivacy(other.uid);
      require(survivor.auth["user.id"] === 1 &&
        survivor.r2_objects >
          0, "privacy erasure crossed persisted account boundary");
      writeFileSync(
        resolve(metadata.trace_dir, "privacy-storage-results.json"),
        JSON.stringify(
          {
            boundary:
              "read-only local Wrangler SQLite; production delays and actual queue; hosted Vectorize not exercised",
            before: beforeDeletion,
            erased,
            survivor,
            release_qualified: false,
          },
          null,
          2,
        ) + "\n",
        { mode: 0o600 },
      );
    },
  );
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
