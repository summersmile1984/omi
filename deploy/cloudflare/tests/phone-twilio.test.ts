import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import jobs from "../workers/jobs/index";
import type { JobMessage } from "../workers/jobs/env";
import { createSignedAuthContext } from "../workers/shared/auth-context";
import { validateTwilioSignature } from "../workers/jobs/phone-twilio";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    for (const filename of readdirSync(directory)
      .filter((value) => value.endsWith(".sql"))
      .sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const bind = (args: unknown[] = []) => ({
      bind: (...values: unknown[]) => bind(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...(args as never[])) as T | undefined) ?? null,
      all: async <T>() => ({
        results: this.database.prepare(sql).all(...(args as never[])) as T[],
      }),
      run: async () => ({
        meta: {
          changes: Number(this.database.prepare(sql).run(...(args as never[])).changes),
        },
      }),
    });
    return bind();
  }
}

const SECRET = "phone-internal-secret";
const TWILIO_AUTH = "twilio-auth-token";

async function authHeaders(method: "GET" | "POST" | "DELETE", pathname: string, uid = "phone-user") {
  const signed = await createSignedAuthContext(
    { uid, authority: "better-auth", requestId: "phone-test" },
    "jobs",
    method,
    pathname,
    SECRET,
  );
  return {
    "x-omi-auth-context": signed!.encoded,
    "x-omi-internal-signature": signed!.signature,
  };
}

function environment() {
  const database = new SqliteD1();
  database.database.exec(
    "INSERT INTO cf_account_cutover (uid, state, checkpoint_phase, destination_backend_bound, updated_at) VALUES ('phone-user', 'new', 'completed', 1, 1), ('other-user', 'new', 'completed', 1, 1);" +
      "INSERT INTO cf_user_subscriptions (uid, plan, status, updated_at) VALUES ('phone-user', 'plus', 'active', 1), ('other-user', 'plus', 'active', 1);",
  );
  const providerCalls: Array<{ method: string; url: string; headers: Headers }> = [];
  const env = {
    APP_DB: database,
    INTERNAL_ASSERTION_SECRET: SECRET,
    PHONE_DATA_ENCRYPTION_SECRET: "phone-encryption-secret",
    TWILIO_ACCOUNT_SID: "ACtwilioplaceholdertwilioplacehold",
    TWILIO_AUTH_TOKEN: TWILIO_AUTH,
    TWILIO_API_KEY_SID: "SKtwilioplaceholdertwilioplacehold",
    TWILIO_API_KEY_SECRET: "api-key-secret",
    TWILIO_TWIML_APP_SID: "APtwilioplaceholdertwilioplacehold",
    PUBLIC_API_BASE_URL: "https://edge.test",
    JOBS: { send: async (_message: JobMessage) => undefined },
  };
  return { database, env, providerCalls };
}

function twilioProvider(providerCalls: Array<{ method: string; url: string; headers: Headers }>, verified = true) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method || (input instanceof Request ? input.method : "GET");
    providerCalls.push({ method, url, headers: new Headers(init?.headers) });
    if (method === "POST") {
      return Response.json({
        sid: "VL1234567890",
        call_sid: "CA1234567890",
        validation_code: "123456",
        status: "pending",
      });
    }
    if (method === "DELETE") return new Response(null, { status: 204 });
    return Response.json({
      outgoing_caller_ids: verified
        ? [{ sid: "PN1234567890", phone_number: "+15551234567", friendly_name: "home" }]
        : [],
    });
  });
}

describe("Cloudflare Phone/Twilio boundary", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("owns verification, D1 projection, Voice JWT and signed TwiML", async () => {
    const { database, env, providerCalls } = environment();
    const provider = twilioProvider(providerCalls);
    vi.stubGlobal("fetch", provider);
    const verify = await jobs.fetch(
      new Request("https://jobs.test/v1/phone/numbers/verify", {
        method: "POST",
        headers: { ...await authHeaders("POST", "/v1/phone/numbers/verify"), "content-type": "application/json" },
        body: JSON.stringify({ phone_number: "+1 (555) 123-4567" }),
      }),
      env as never,
    );
    expect(verify.status).toBe(200);
    expect(await verify.json()).toMatchObject({ verification_sid: "CA1234567890", validation_code: "123456", status: "pending" });
    expect(providerCalls[0].headers.get("authorization")).toMatch(/^Basic [A-Za-z0-9+/]+=*$/);

    const duplicate = await jobs.fetch(
      new Request("https://jobs.test/v1/phone/numbers/verify", {
        method: "POST",
        headers: { ...await authHeaders("POST", "/v1/phone/numbers/verify"), "content-type": "application/json" },
        body: JSON.stringify({ phone_number: "+15551234567" }),
      }),
      env as never,
    );
    expect(duplicate.status).toBe(409);

    const checked = await jobs.fetch(
      new Request("https://jobs.test/v1/phone/numbers/verify/check", {
        method: "POST",
        headers: { ...await authHeaders("POST", "/v1/phone/numbers/verify/check"), "content-type": "application/json" },
        body: JSON.stringify({ phone_number: "+15551234567" }),
      }),
      env as never,
    );
    const checkedBody = (await checked.json()) as { verified: boolean; phone_number_id?: string };
    expect(checked.status).toBe(200);
    expect(checkedBody.verified).toBe(true);
    expect(checkedBody.phone_number_id).toMatch(/^[0-9a-f-]{36}$/);

    const listed = await jobs.fetch(
      new Request("https://jobs.test/v1/phone/numbers", { headers: await authHeaders("GET", "/v1/phone/numbers") }),
      env as never,
    );
    expect(await listed.json()).toMatchObject({ numbers: [{ phone_number: "+15551234567", is_primary: true }] });

    const token = await jobs.fetch(
      new Request("https://jobs.test/v1/phone/token", { method: "POST", headers: await authHeaders("POST", "/v1/phone/token") }),
      env as never,
    );
    const tokenBody = (await token.json()) as { access_token: string; identity: string };
    const tokenParts = tokenBody.access_token.split(".");
    expect(tokenParts).toHaveLength(3);
    const tokenEncoded = tokenParts[1].replace(/-/g, "+").replace(/_/g, "/");
    const tokenPayload = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(tokenEncoded + "===".slice((tokenEncoded.length + 3) % 4)), (character) => character.charCodeAt(0)))) as Record<string, unknown>;
    expect(tokenPayload).toMatchObject({ sub: env.TWILIO_ACCOUNT_SID, iss: env.TWILIO_API_KEY_SID, "grants": { identity: "phone-user" } });

    const params = new URLSearchParams({ To: "+15557654321", From: "client:phone-user", CallId: "CA-call" });
    const twimlUrl = "https://edge.test/v1/phone/twiml";
    const valid = await validateTwilioSignature(twimlUrl, params, "", TWILIO_AUTH);
    expect(valid).toBe(false);
    const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(TWILIO_AUTH), { name: "HMAC", hash: "SHA-1" }, false, ["sign"]);
    const digest = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${twimlUrl}CallIdCA-callFromclient:phone-userTo+15557654321`)));
    let binary = "";
    for (const byte of digest) binary += String.fromCharCode(byte);
    const twilioSignature = btoa(binary);
    const twiml = await jobs.fetch(
      new Request("https://jobs.test/v1/phone/twiml", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": twilioSignature }, body: params.toString() }),
      env as never,
    );
    expect(twiml.status).toBe(200);
    const twimlBody = await twiml.text();
    expect(twimlBody).toContain('callerId="+15551234567"');
    expect(twimlBody).toContain("+15557654321");

    // Twilio can retry the same signed webhook. The provider call id is
    // hashed in D1, so a retry returns the same dial instruction without
    // creating another call-attempt reservation.
    const repeated = await jobs.fetch(
      new Request("https://jobs.test/v1/phone/twiml", {
        method: "POST",
        headers: {
          "content-type": "application/x-www-form-urlencoded",
          "x-twilio-signature": twilioSignature,
        },
        body: params.toString(),
      }),
      env as never,
    );
    expect(repeated.status).toBe(200);
    expect(await repeated.text()).toBe(twimlBody);
    expect(
      database.database.prepare("SELECT COUNT(*) AS count FROM cf_phone_call_attempts").get(),
    ).toMatchObject({ count: 1 });

    const removed = await jobs.fetch(
      new Request(`https://jobs.test/v1/phone/numbers/${checkedBody.phone_number_id}`, { method: "DELETE", headers: await authHeaders("DELETE", `/v1/phone/numbers/${checkedBody.phone_number_id}`) }),
      env as never,
    );
    expect(removed.status).toBe(200);
    expect(await removed.json()).toEqual({ success: true });
    expect(providerCalls.some(({ method }) => method === "DELETE")).toBe(true);
    database.database.close();
  });

  it("does not permit cross-account pending claims and fences all writes during deletion", async () => {
    const { database, env, providerCalls } = environment();
    const provider = twilioProvider(providerCalls);
    vi.stubGlobal("fetch", provider);
    const first = await jobs.fetch(
      new Request("https://jobs.test/v1/phone/numbers/verify", {
        method: "POST",
        headers: { ...await authHeaders("POST", "/v1/phone/numbers/verify"), "content-type": "application/json" },
        body: JSON.stringify({ phone_number: "+15551234567" }),
      }),
      env as never,
    );
    expect(first.status).toBe(200);
    const other = await jobs.fetch(
      new Request("https://jobs.test/v1/phone/numbers/verify", {
        method: "POST",
        headers: { ...await authHeaders("POST", "/v1/phone/numbers/verify", "other-user"), "content-type": "application/json" },
        body: JSON.stringify({ phone_number: "+15551234567" }),
      }),
      env as never,
    );
    expect(other.status).toBe(409);
    database.database.exec("INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES ('other-user', 'delete-other', 'pending', 'quiescing', 1, 1, 1);");
    const fenced = await jobs.fetch(
      new Request("https://jobs.test/v1/phone/numbers/verify", {
        method: "POST",
        headers: { ...await authHeaders("POST", "/v1/phone/numbers/verify", "other-user"), "content-type": "application/json" },
        body: JSON.stringify({ phone_number: "+15557654321" }),
      }),
      env as never,
    );
    expect(fenced.status).toBe(409);
    expect(provider).toHaveBeenCalledTimes(1);
    database.database.close();
  });
});
