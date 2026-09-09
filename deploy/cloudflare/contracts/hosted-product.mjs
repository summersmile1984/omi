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
  { target = "cloudflare", spawn = spawnSync } = {}
) {
  const origins = hostedOrigins(context.candidate, target);
  const output = mkdtempSync(resolve(tmpdir(), "eddy-hosted-core-"));
  const metadata = {
    target,
    brand_id: context.candidate.brand,
    trace_dir: output,
    api_origin: origins.api,
    auth_origin: origins.auth,
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
  });
}

export async function runHostedCloudflare(
  context,
  { fetchImpl = fetch, smoke = runSmoke } = {}
) {
  context.verify();
  require(context.observations.release_phase ===
    "deployed", "hosted checks require observed deployed versions");
  const origins = hostedOrigins(context.candidate);
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
    cases.push(...Object.keys(result).map((key) => `hosted.routes.${key}`));

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
      const socket = new WebSocket(
        `${origins.api.replace(
          "https:",
          "wss:"
        )}${path}?codec=pcm16&sample_rate=16000&language=en&client_conversation_id=${randomUUID()}`,
        native ? { headers: { Authorization: `Bearer ${owner.token}` } } : {}
      );
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
  } finally {
    // Only accounts created by this invocation are eligible for deletion.
    const failures = [];
    for (const account of accounts) {
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
      } catch {
        failures.push(account);
      }
    }
    require(failures.length === 0, "synthetic release account cleanup failed");
  }
  context.verify();
  return cases;
}
