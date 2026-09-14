import { randomUUID, createHash } from "node:crypto";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import WebSocket from "ws";
import { runSmoke } from "../scripts/smoke-staging.mjs";
import { readProductReport } from "./product-regression.mjs";

function require(value, message) {
  if (!value) throw new Error(message);
}

export function hostedOrigins(candidate, target = "cloudflare") {
  const profile = candidate.profiles[target].profile;
  const origins = Object.fromEntries(
    ["api", "auth", "web"].map((key) => {
      const url = new URL(profile[`${key}_base_url`]);
      require(url.protocol === "https:" &&
        !url.username &&
        !url.password &&
        url.pathname === "/" &&
        !url.search &&
        !url.hash, "hosted qualification requires the frozen profile's exact HTTPS origins");
      return [key, url.origin];
    })
  );
  require(new Set(Object.values(origins)).size ===
    3, "API, Auth and Web origins must be distinct");
  return origins;
}

export function runHostedCore(
  context,
  { target = "cloudflare", spawn = spawnSync, transport } = {}
) {
  const origins = hostedOrigins(context.candidate, target);
  const output = mkdtempSync(resolve(tmpdir(), "eddy-hosted-core-"));
  const metadata = {
    target,
    brand_id: context.candidate.brand,
    trace_dir: output,
    api_origin: origins.api,
    auth_origin: origins.auth,
    ...(transport?.metadata ?? {}),
  };
  const path = resolve(output, "metadata.json");
  writeFileSync(path, JSON.stringify(metadata), { mode: 0o600 });
  const result = spawn(
    resolve(context.root, "backend/.venv/bin/python"),
    [
      resolve(context.root, "contracts/deployment/core.py"),
      "--metadata",
      path,
      "--remote",
    ],
    {
      cwd: context.root,
      encoding: "utf8",
      timeout: 8 * 60 * 1000,
      maxBuffer: 4 * 1024 * 1024,
    }
  );
  require(result.status ===
    0, `hosted common business contract failed; inspect ${output}/core-results.json`);
  return readProductReport(resolve(output, "core-results.json"), {
    target,
    brand: context.candidate.brand,
    suite: 'core', surface: 'frozen', remote: true,
  });
}

export async function runHostedCloudflare(
  context,
  { fetchImpl = fetch, smoke = runSmoke, transport } = {}
) {
  context.verify();
  require(context.observations.release_phase ===
    "deployed", "hosted checks require observed deployed versions");
  const origins = hostedOrigins(context.candidate);
  fetchImpl = transport?.fetch ?? fetchImpl;
  const cases = [],
    accounts = [];
  async function request(
    service,
    path,
    status,
    { method = "GET", token, body, raw, headers = {} } = {}
  ) {
    const response = await fetchImpl(origins[service] + path, {
      method,
      redirect: "error",
      signal: AbortSignal.timeout(120000),
      headers: {
        Origin: origins.web,
        "X-App-Platform": "web",
        ...headers,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: raw ?? (body === undefined ? undefined : JSON.stringify(body)),
    });
    // Do not include paths, response bodies or headers in exceptions: some
    // product paths contain bearer capability links and account identifiers.
    require(response.status ===
      status, `hosted ${service} ${method}: HTTP ${response.status}, expected ${status}`);
    return response;
  }
  async function signup() {
    const response = await request("auth", "/api/auth/sign-up/email", 200, {
      method: "POST",
      body: {
        email: `release-${randomUUID()}@example.invalid`,
        password: randomUUID() + randomUUID(),
        name: "Release qualification",
      },
    });
    const body = await response.json(),
      session = response.headers.get("set-auth-token");
    require(body.user?.id && session, "signup omitted owned identity/session");
    const account = { uid: body.user.id, session };
    accounts.push(account);
    account.token = (
      await (
        await request("auth", "/api/auth/token", 200, { token: session })
      ).json()
    ).token;
    require(account.token, "JWT exchange failed");
    return account;
  }
  try {
    const owner = await signup(),
      other = await signup();
    const result = await smoke({
      edgeUrl: origins.api,
      webUrl: origins.web,
      token: owner.token,
      nativeTts: true,
      fetchImpl,
    });
    require(result.nativeTts === 200 &&
      result.webProxyWorkersAiChat ===
        200, "hosted qualification must execute native TTS and Web streamed model chat");
    cases.push("hosted.routes.complete-route-smoke", "hosted.providers.web-workers-ai-chat");

    const bytes = Buffer.from(`owned synthetic release object ${randomUUID()}`);
    const asset = `/v1/cf/assets/release-${randomUUID()}.txt`;
    await request("api", asset, 200, {
      method: "PUT",
      token: owner.token,
      raw: bytes,
      headers: {
        "Content-Type": "text/plain",
        "x-content-sha256": createHash("sha256").update(bytes).digest("hex"),
      },
    });
    require(Buffer.from(
      await (
        await request("api", asset, 200, { token: owner.token })
      ).arrayBuffer()
    ).equals(bytes), "hosted R2 object bytes differ");
    await request("api", asset, 404, { token: other.token });
    cases.push("hosted.r2.write-read-owner-isolation");

    const key = await (
      await request("api", "/v1/mcp/keys", 200, {
        method: "POST",
        token: owner.token,
        body: { name: "Release qualification" },
      })
    ).json();
    const rpc = await request("api", "/v1/mcp/sse", 200, {
      method: "POST",
      token: key.key,
      body: { jsonrpc: "2.0", id: 1, method: "tools/list" },
      headers: {
        Accept: "application/json, text/event-stream",
        "mcp-protocol-version": "2025-03-26",
      },
    });
    const rpcText = await rpc.text();
    const dataLine = rpcText
      .split("\n")
      .find((line) => line.startsWith("data:"));
    const data = dataLine ? dataLine.slice(5) : rpcText;
    require(Array.isArray(JSON.parse(data).result?.tools) &&
      JSON.parse(data).result.tools.length >
        0, "MCP did not advertise tools through the real transport");
    await request("api", `/v1/mcp/keys/${key.id}`, 204, {
      method: "DELETE",
      token: owner.token,
    });
    await request("api", "/v1/mcp/sse", 401, {
      method: "POST",
      token: key.key,
      body: { jsonrpc: "2.0", id: 2, method: "tools/list" },
    });
    cases.push("hosted.mcp.tools-and-revocation");

    for (const native of [true, false]) {
      const path = native ? "/v4/listen" : "/v4/web/listen";
      const socketArguments = [
        `${origins.api.replace(
          "https:",
          "wss:"
        )}${path}?codec=pcm16&sample_rate=16000&language=en&client_conversation_id=${randomUUID()}`,
        native ? { headers: { Authorization: `Bearer ${owner.token}` } } : {}
      ];
      const socket = new WebSocket(...(transport ? transport.socket(...socketArguments) : socketArguments));
      try {
        await new Promise((accept, reject) => {
          const timeout = setTimeout(
            () =>
              reject(new Error("hosted recording session deadline exceeded")),
            30000
          );
          const finish = (error) => {
            clearTimeout(timeout);
            error ? reject(error) : accept();
          };
          socket.once("error", () =>
            finish(new Error("hosted recording transport failed"))
          );
          socket.once("open", () => {
            if (!native)
              socket.send(JSON.stringify({ type: "auth", token: owner.token }));
          });
          socket.on("message", (raw) => {
            try {
              if (JSON.parse(raw.toString()).type === "conversation_session")
                finish();
            } catch {
              finish(new Error("recording returned an invalid frame"));
            }
          });
        });
      } finally {
        socket.terminate();
      }
      cases.push(
        `hosted.recording.${native ? "native" : "web"}-authenticated-session`
      );
    }
    const speech = await request("api", "/v1/tts/synthesize-workers-ai", 200, {
      method: "POST",
      token: owner.token,
      body: {
        text: "This is an automated deployment test. Remember to check the project tomorrow.",
        speaker: "luna",
      },
    });
    const audio = Buffer.from(await speech.arrayBuffer());
    require(audio.length > 100, "native TTS returned empty audio");
    const transcript = await (
      await request("api", "/v1/stt/transcribe-workers-ai", 200, {
        method: "POST",
        token: owner.token,
        raw: audio,
        headers: { "Content-Type": "audio/mpeg" },
      })
    ).json();
    require(typeof transcript.text === "string" &&
      transcript.text.trim().length > 0 &&
      !transcript.error, "native ASR returned no transcript");
    cases.push("hosted.providers.tts-to-asr");
    const decoded = spawnSync('ffmpeg', ['-v','error','-i','pipe:0','-f','s16le','-ac','1','-ar','16000','pipe:1'], {
      input: audio, timeout:30000, maxBuffer:8*1024*1024,
    });
    require(decoded.status === 0 && decoded.stdout.length > 32000, 'recording fixture PCM conversion failed');
    const recordingId = randomUUID();
    const socketArgs = [`${origins.api.replace('https:', 'wss:')}/v4/listen?codec=pcm16&sample_rate=16000&language=en&client_conversation_id=${recordingId}`,
      { headers: { Authorization: `Bearer ${owner.token}` } }];
    const recording = new WebSocket(...(transport ? transport.socket(...socketArgs) : socketArgs));
    try {
      await new Promise((accept, reject) => {
        const timer = setTimeout(() => finish(new Error('recording transcript deadline exceeded')), 90000);
        let sent = false, done = false;
        const finish = error => { if (done) return; done = true; clearTimeout(timer); error ? reject(error) : accept(); };
        recording.once('error', () => finish(new Error('recording audio transport failed')));
        recording.once('close', () => finish(new Error('recording closed before persisted transcript')));
        recording.on('message', raw => {
          try {
            const frame = JSON.parse(raw.toString());
            if (frame.type === 'conversation_session' && !sent) {
              sent = true;
              // A single bounded synthetic utterance, followed by silence to
              // let the real provider close its final transcription segment.
              recording.send(Buffer.concat([decoded.stdout, Buffer.alloc(32000)]));
            }
            if (Array.isArray(frame) && frame.some(row => typeof row.text === 'string' && row.text.trim())) finish();
            if (frame.type === 'error') finish(new Error('recording provider rejected audio'));
          } catch { finish(new Error('recording returned an invalid transcript frame')); }
        });
      });
    } finally { recording.terminate(); }
    const persisted = await (await request('api', `/v1/conversations/${recordingId}`, 200, {token:owner.token})).json();
    require(persisted.transcript_segments?.some(row => row.text?.trim()), 'recording transcript was not persisted before broadcast');
    await request('api', `/v1/conversations/${recordingId}/finalize`, 200, {method:'POST',token:owner.token,body:{}});
    const deadline = Date.now() + 180000;
    for (;;) {
      const stored = await (await request('api', `/v1/conversations/${recordingId}`, 200, {token:owner.token})).json();
      if (stored.status === 'completed') break;
      require(stored.status !== 'failed' && Date.now() < deadline, 'real Queue/provider finalization did not complete');
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    await request('api', `/v1/conversations/${recordingId}`, 404, {token:other.token});
    cases.push('hosted.recording.audio-persistence-queue-finalization');
  } finally {
    // Only accounts created by this invocation are eligible for deletion.
    const failures = [];
    for (const [index, account] of accounts.entries()) {
      try {
        const token =
          account.token ||
          (
            await (
              await request("auth", "/api/auth/token", 200, {
                token: account.session,
              })
            ).json()
          ).token;
        await request("api", "/v1/users/delete-account", 200, {
          method: "DELETE",
          token,
          body: {},
        });
        cases.push(`hosted.cleanup.synthetic-account-${index}`);
      } catch {
        failures.push(account);
      }
    }
    require(failures.length === 0, "synthetic release account cleanup failed");
  }
  context.verify();
  return cases;
}
