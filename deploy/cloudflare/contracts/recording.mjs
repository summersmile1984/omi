import { createHash, createHmac, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
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
let unsubscribeToken;
function issueUnsubscribeToken(uid) {
  // Only this disposable runner's private issuer key; never a session bypass
  // or database seed. The public Python route verifies the real capability.
  const vars = readFileSync(resolve(dirname(resolve(values.metadata)), "workers/api-core/.dev.vars"), "utf8");
  const secret = vars.match(/^LIFECYCLE_EMAIL_SIGNING_SECRET=([a-f0-9]{64})$/m)?.[1];
  if (!secret) throw new Error("local lifecycle issuer secret is missing");
  return Buffer.from(uid).toString("base64url") + "." +
    createHmac("sha256", secret).update(`${uid}:lifecycle`).digest("base64url");
}
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
    route: path.startsWith("/email/unsubscribe?") ? "/email/unsubscribe" : path.replace(/[0-9a-f]{8}-[0-9a-f-]{27}/g, ":id"),
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
  await caseOf("recording.realtime-usage-idempotent-cost-and-export", async () => {
    const before = await request("api", "/v1/users/me/llm-usage/total", 200, { token: owner.token });
    const quota = await request("api", "/v1/users/me/usage-quota", 200, { token: owner.token });
    const body = {
      provider: "openai", model: "caller-cannot-select-rates", turn_id: randomUUID(),
      input_text_tokens: 100, input_cached_tokens: 40,
    };
    await request("api", "/v2/realtime/usage", 401, { method: "POST", body, bytes: true });
    await Promise.all(Array.from({ length: 4 }, () => request("api", "/v2/realtime/usage", 204, {
      token: owner.token, method: "POST", body, bytes: true,
    })));
    await request("api", "/v2/realtime/usage", 204, {
      token: owner.token, method: "POST", body: { ...body, input_text_tokens: 200 }, bytes: true,
    });
    const after = await request("api", "/v1/users/me/llm-usage/total", 200, { token: owner.token });
    require(Math.abs(after.data.total_cost_usd - before.data.total_cost_usd - 0.000256) < 1e-9,
      "realtime retry or cached input changed the charged cost");
    const used = await request("api", "/v1/users/me/usage-quota", 200, { token: owner.token });
    require(used.data.used === quota.data.used + 1, "one realtime turn did not consume exactly one question");
    await Promise.all(["workers-ai", "cloudflare-workers-ai"].map((provider) =>
      request("api", "/v2/realtime/usage", 204, {
        token: owner.token, method: "POST", body: { ...body, provider }, bytes: true,
      })));
    const nativeQuota = await request("api", "/v1/users/me/usage-quota", 200, { token: owner.token });
    require(nativeQuota.data.used === used.data.used, "native speech metadata charged a chat question");
    const exported = await request("api", "/v1/users/export", 200, { token: owner.token });
    require(exported.data.realtime_turns.length === 2, "realtime turn receipts were duplicated or missing");
    const managed = exported.data.realtime_turns.find((row) => row.provider === "openai");
    require(managed?.cost_micros === 256 && managed?.input_text_tokens === 100 &&
      managed?.model === "gpt-realtime-2" && !managed?.idempotency_key.includes(body.turn_id),
      "realtime receipt did not preserve the first normalized report");
    const otherExport = await request("api", "/v1/users/export", 200, { token: other.token });
    require(otherExport.data.realtime_turns.length === 0, "realtime usage crossed account ownership");
  });
  await caseOf("referral.branded-link-atomic-trial-and-export", async () => {
    const link = await request("api", "/v1/users/me/referral", 200, { token: other.token });
    const url = new URL(link.data.referral_url);
    require(url.origin === metadata.api_origin, "referral escaped the selected API origin");
    const capture = await fetch(url, { redirect: "manual", signal: AbortSignal.timeout(20000) });
    require(capture.status === 302, "referral capture did not redirect");
    const redirect = new URL(capture.headers.get("location"));
    require(redirect.pathname === "/login" && redirect.hostname === "127.0.0.1",
      "referral did not use the configured local Web login");
    const cookie = capture.headers.get("set-cookie") ?? "";
    require(cookie.includes("HttpOnly") && cookie.includes("Secure"), "referral cookie lost its protections");
    await capture.body?.cancel();
    const code = redirect.searchParams.get("referral");
    const claims = await Promise.all(Array.from({ length: 4 }, () => request("api",
      "/v1/users/me/referral/claim", 200, { token: owner.token, method: "POST", body: { code } })));
    require(claims.filter((item) => item.data.claimed).length === 1, "referral grant did not have exactly one winner");
    const subscription = await request("api", "/v1/users/me/subscription", 200, { token: owner.token });
    require(subscription.data.subscription.plan === "operator" &&
      subscription.data.subscription.current_period_end - subscription.data.subscription.current_period_start === 2592000,
      "referral did not publish the thirty-day Operator entitlement");
    const exported = await request("api", "/v1/users/export", 200, { token: owner.token });
    require(exported.data.referral_claims.length === 1 && exported.data.referral_attributions.length === 1,
      "referral export lost its owned receipt");
    const otherExport = await request("api", "/v1/users/export", 200, { token: other.token });
    require(otherExport.data.referral_claims.length === 0, "referral receipt crossed accounts");
  });
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
  // Finish recap coverage while the derived memory is processed, then verify
  // explicit correction revokes its index eligibility. Both contracts remain asserted.
  await caseOf("recording.memory-vector-search-fences-old-revisions", async () => {
    const listed = await request("api", "/v3/memories", 200, { token: owner.token });
    const memory = listed.data.find((row) => row.content.includes("prefers concise updates"));
    require(memory, "derived memory missing before vector search");
    const searchPath = "/memory/vector/search?query=concise&limit=10";
    async function waitForContent(content) {
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        const result = await request("api", searchPath, 200, { token: owner.token });
        const item = result.data.items.find((row) => row.id === memory.id);
        if (item) {
          require(item.content === content, "stale vector returned the wrong canonical content");
          const revision = Number(result.data.projection_commit_ids_by_memory_id[memory.id]);
          require(Number.isSafeInteger(revision) && revision > 0, "vector result lost its canonical revision");
          return revision;
        }
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      throw new Error("actual queue did not publish the current memory revision");
    }
    const firstRevision = await waitForContent(memory.content);
    const content = `${memory.content} Updated by the recording contract.`;
    await request("api", `/v3/memories/${memory.id}`, 200, {
      token: owner.token, method: "PATCH", body: { value: content },
    });
    // Upstream update_canonical_memory_content returns user corrections to
    // pending Short-term. Public export proves the mutation without inventing
    // processor admission or requiring an ineligible item to be indexed.
    const exported = await request("api", "/v1/users/export", 200, { token: owner.token });
    const corrected = exported.data.memories.find((row) => row.id === memory.id);
    require(corrected?.content === content && corrected.memory_tier === "short_term" &&
      corrected.processing_state === "pending", "correction lost its pending Short-term state");
    require(corrected.item_revision > firstRevision, "memory edit reused its prior vector revision");
    const pending = await request("api", searchPath, 200, { token: owner.token });
    require(pending.data.items.every((row) => row.id !== memory.id) &&
      pending.data.projection_commit_ids_by_memory_id[memory.id] === undefined,
      "pending correction retained an eligible vector result");
    const isolated = await request("api", searchPath, 200, { token: other.token });
    require(isolated.data.items.every((row) => row.id !== memory.id), "memory vector crossed account boundary");
  });
  await caseOf(
    "email.scanner-safe-token-opt-out-and-export",
    async () => {
      unsubscribeToken = issueUnsubscribeToken(owner.uid);
      const path = `/email/unsubscribe?token=${encodeURIComponent(unsubscribeToken)}`;
      const before = await request("api", "/v1/users/export", 200, { token: owner.token });
      require(before.data.email_preferences.length === 0, "new account already has lifecycle preferences");
      for (let index = 0; index < 2; index++) {
        const page = await request("api", path, 200, { bytes: true });
        require(page.data.toString().includes('<form method="post"'), "scanner GET omitted confirmation form");
        require(page.headers.get("cache-control") === "no-store" && page.headers.get("referrer-policy") === "no-referrer", "unsubscribe leaked cache/referrer state");
      }
      const scanned = await request("api", "/v1/users/export", 200, { token: owner.token });
      require(scanned.data.email_preferences.length === 0, "scanner GET changed consent");
      for (let index = 0; index < 2; index++) {
        const result = await request("api", path, 200, {
          method: "POST", rawBody: "List-Unsubscribe=One-Click", bytes: true,
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
        });
        require(!result.data.toString().includes(unsubscribeToken), "POST echoed the unsubscribe token");
      }
      const invalid = await request("api", "/email/unsubscribe?token=invalid", 400, { method: "POST", bytes: true });
      const missing = await request("api", `/email/unsubscribe?token=${issueUnsubscribeToken(randomUUID())}`, 400, { method: "POST", bytes: true });
      require(invalid.data.equals(missing.data), "unsubscribe revealed account existence");
    },
  );
  await caseOf(
    "recording.export-restored-recording-memory-task-and-profile",
    async () => {
      await request("api", "/v1/users/export", 401);
      await request("api", "/v1/csat/ratings", 201, {
        token: owner.token, method: "POST",
        body: { platform: "macos", score: 2, comment: "Synthetic private feedback" },
      });
      const exported = await request("api", "/v1/users/export", 200, {
        token: owner.token,
      });
      require(exported.headers.get("content-disposition") ===
        `attachment; filename="${metadata.brand_id}-export.json"`, "export filename ignored the configured brand");
      require(exported.data.csat_ratings.length === 1 &&
        exported.data.csat_ratings[0].comment === "Synthetic private feedback",
        "export lost owned CSAT rating");
      require(exported.data.email_preferences.length === 1 &&
        exported.data.email_preferences[0].lifecycle_opted_out === 1,
        "export lost token-owned lifecycle opt-out");
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
      require(isolated.data.csat_ratings.length === 0, "CSAT export crossed account boundary");
      require(isolated.data.email_preferences.length === 0, "email consent crossed account boundary");
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
  await caseOf("recording.cascade-delete-retracts-derived-memory-and-tasks", async () => {
    const cascadeId = randomUUID();
    const capture = await connect(other.token, cascadeId);
    capture.socket.send(new Uint8Array(16000));
    await capture.until((frames) => frames.some(Array.isArray));
    capture.socket.close();
    await new Promise((resolve) => capture.socket.once("close", resolve));
    await request("api", `/v1/conversations/${cascadeId}/finalize`, 200, {
      token: other.token,
      method: "POST",
      body: {},
    });
    let completed = false;
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      const stored = await request("api", `/v1/conversations/${cascadeId}`, 200, {
        token: other.token,
      });
      if (stored.data.status === "completed") {
        completed = true;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    require(completed, "cascade fixture did not finalize through the queue");
    const memories = await request("api", "/v3/memories", 200, { token: other.token });
    const tasks = await request("api", "/v1/action-items", 200, { token: other.token });
    const memoryIds = memories.data
      .filter((row) => row.conversation_id === cascadeId).map((row) => row.id);
    const taskIds = tasks.data.action_items
      .filter((row) => row.conversation_id === cascadeId).map((row) => row.id);
    require(memoryIds.length > 0 && taskIds.length > 0,
      "cascade fixture has no persisted derived data");
    await request("api", `/v1/conversations/${cascadeId}?cascade=true`, 404, {
      token: owner.token, method: "DELETE",
    });
    await request("api", `/v1/conversations/${cascadeId}?cascade=true`, 200, {
      token: other.token, method: "DELETE",
    });
    await request("api", `/v1/conversations/${cascadeId}`, 404, { token: other.token });
    const remainingMemories = await request("api", "/v3/memories", 200, { token: other.token });
    const remainingTasks = await request("api", "/v1/action-items", 200, { token: other.token });
    require(!remainingMemories.data.some((row) => memoryIds.includes(row.id)),
      "cascade retained derived memory");
    require(!remainingTasks.data.action_items.some((row) => taskIds.includes(row.id)),
      "cascade retained derived task");
    await request("api", `/v1/conversations/${id}`, 200, { token: owner.token });
  });
  const inspectPrivacy = await localPrivacyObserver(metadata);
  await caseOf("recording.public-memory-delete-waits-for-provider-cleanup", async () => {
    const privacyId = randomUUID();
    const capture = await connect(other.token, privacyId);
    capture.socket.send(new Uint8Array(16000));
    await capture.until((frames) => frames.some(Array.isArray));
    capture.socket.close();
    await new Promise((resolve) => capture.socket.once("close", resolve));
    await request("api", `/v1/conversations/${privacyId}/finalize`, 200, {
      token: other.token, method: "POST", body: {},
    });
    let memory;
    const searchPath = "/memory/vector/search?query=concise&limit=10";
    const indexedDeadline = Date.now() + 30000;
    while (Date.now() < indexedDeadline) {
      const result = await request("api", searchPath, 200, { token: other.token });
      memory = result.data.items.find((row) => row.conversation_id === privacyId);
      if (memory) break;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    require(memory, "privacy recording never became searchable through actual Jobs publication");
    const before = inspectPrivacy(other.uid, memory.id);
    require(before.memory_cleanup.rows === 1 && before.memory_cleanup.artifacts > 0 &&
      before.memory_cleanup.mappings > 0, "privacy fixture lacks a persisted published vector");
    await request("api", `/v3/memories/${memory.id}`, 404, { token: owner.token, method: "DELETE" });
    const removed = await request("api", `/v3/memories/${memory.id}`, [200, 503], {
      token: other.token, method: "DELETE",
    });
    if (removed.status === 503) {
      require(removed.data.error === "memory_cleanup_pending" && removed.headers.get("retry-after") === "2",
        "deletion failed before durable provider cleanup admission");
    }
    const hidden = await request("api", searchPath, 200, { token: other.token });
    require(hidden.data.items.every((row) => row.id !== memory.id), "deleted memory is still searchable");
    // Do not retry the public DELETE here: Jobs must drive its own signed Core
    // continuation and finish the durable request without another client call.
    let after = inspectPrivacy(other.uid, memory.id);
    const cleanupDeadline = Date.now() + 30000;
    while (Object.values(after.memory_cleanup).some((value) => value !== 0) && Date.now() < cleanupDeadline) {
      require(removed.status === 503, "public deletion acknowledged unfinished physical cleanup");
      await new Promise((resolve) => setTimeout(resolve, 250));
      after = inspectPrivacy(other.uid, memory.id);
    }
    require(Object.values(after.memory_cleanup).every((value) => value === 0),
      "Jobs did not finish physical memory, vector journal and request cleanup");
    require(after.app["cf_memory_privacy_receipts.uid"] === before.app["cf_memory_privacy_receipts.uid"] + 1,
      "completed privacy deletion lost its retry receipt");
    await request("api", `/v3/memories/${memory.id}`, 200, { token: other.token, method: "DELETE" });
    await request("api", `/v1/conversations/${privacyId}`, 200, { token: other.token });
    await request("api", `/v1/conversations/${id}`, 200, { token: owner.token });
    writeFileSync(resolve(metadata.trace_dir, "memory-privacy-results.json"), JSON.stringify({
      boundary: "actual Edge/Core/Jobs/D1 with controlled Vectorize", initial_status: removed.status,
      before: before.memory_cleanup, after: after.memory_cleanup, release_qualified: false,
    }, null, 2) + "\n", { mode: 0o600 });
  });
  const unused = await signup();
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
      const retired = await request("api", `/email/unsubscribe?token=${encodeURIComponent(unsubscribeToken)}`, 400, { method: "POST", bytes: true });
      require(!retired.data.toString().includes(unsubscribeToken), "retired account token was echoed");
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
