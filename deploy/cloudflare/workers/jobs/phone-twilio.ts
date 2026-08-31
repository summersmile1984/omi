import type { Context, Hono } from "hono";
import type { JobsEnv } from "./env";

const MAX_BODY_BYTES = 16_384;
const MAX_TWIML_BODY_BYTES = 32_768;
const VERIFICATION_TTL_SECONDS = 5 * 60;
const CALL_ATTEMPT_TTL_SECONDS = 60;
const TOKEN_TTL_SECONDS = 60 * 60;
const MAX_TOKEN_TTL_SECONDS = 24 * 60 * 60;
const E164 = /^\+[1-9]\d{1,14}$/;
const UID = /^[^/\0]{1,256}$/;
const SID = /^[A-Za-z]{2}[A-Za-z0-9]{8,160}$/;
const PAID_PLANS = new Set([
  "unlimited",
  "plus",
  "unlimited_v2",
  "operator",
  "architect",
]);
const E164_COUNTRIES: Array<[string, string[]]> = [
  ["+1", ["US", "CA"]],
  ["+44", ["GB"]],
  ["+61", ["AU"]],
  ["+64", ["NZ"]],
  ["+33", ["FR"]],
  ["+49", ["DE"]],
  ["+34", ["ES"]],
  ["+39", ["IT"]],
  ["+31", ["NL"]],
  ["+46", ["SE"]],
  ["+47", ["NO"]],
  ["+45", ["DK"]],
  ["+358", ["FI"]],
  ["+353", ["IE"]],
  ["+41", ["CH"]],
  ["+43", ["AT"]],
  ["+32", ["BE"]],
  ["+351", ["PT"]],
  ["+81", ["JP"]],
  ["+82", ["KR"]],
];

type JobsContext = Context<{ Bindings: JobsEnv }>;
type AuthContext = { uid: string; authority?: string };

type PhoneRow = {
  uid: string;
  id: string;
  phone_number_hash: string;
  phone_number_ciphertext: string;
  friendly_name: string | null;
  twilio_sid: string | null;
  verified_at: number;
  is_primary: number;
  account_generation: number;
};

type VerificationRow = {
  verification_id: string;
  uid: string;
  phone_number_hash: string;
  phone_number_ciphertext: string;
  twilio_validation_sid: string | null;
  status: "pending" | "failed" | "expired";
  account_generation: number;
  attempts: number;
  expires_at: number;
};

type PolicyRow = {
  free_monthly_limit: number | null;
  free_max_duration_seconds: number | null;
  free_allowed_countries_json: string;
  paid_monthly_limit: number | null;
  paid_max_duration_seconds: number | null;
  paid_allowed_countries_json: string;
};

class PhoneHttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

class TwilioError extends Error {
  constructor(
    readonly status: number,
    readonly code: string | null,
    message: string,
  ) {
    super(message);
  }
}

function jsonObject(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function validUid(uid: string): boolean {
  return UID.test(uid);
}

function normalizePhone(value: unknown): string {
  if (typeof value !== "string")
    throw new PhoneHttpError(400, "invalid_phone_number", "phone number is required");
  const phone = value.trim().replace(/[\s\-().]+/g, "");
  if (!E164.test(phone))
    throw new PhoneHttpError(400, "invalid_phone_number", "phone number must be in E.164 format");
  return phone;
}

function nowSeconds(): number {
  return Math.floor(Date.now() / 1_000);
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function base64Standard(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function base64UrlDecode(value: string): Uint8Array {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  let difference = leftBytes.length ^ rightBytes.length;
  const max = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < max; index += 1) {
    difference |= (leftBytes[index] || 0) ^ (rightBytes[index] || 0);
  }
  return difference === 0;
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function aesKey(secret: string): Promise<CryptoKey> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(secret));
  return crypto.subtle.importKey("raw", digest, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
}

async function encryptPhone(secret: string, phone: string): Promise<string> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const encrypted = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    await aesKey(secret),
    new TextEncoder().encode(phone),
  );
  return `${base64Url(iv)}.${base64Url(new Uint8Array(encrypted))}`;
}

async function decryptPhone(secret: string, ciphertext: string): Promise<string> {
  const [ivPart, valuePart] = ciphertext.split(".");
  if (!ivPart || !valuePart) throw new Error("invalid phone ciphertext");
  const encodedIv = base64UrlDecode(ivPart);
  const iv = new Uint8Array(encodedIv.byteLength);
  iv.set(encodedIv);
  const encodedValue = base64UrlDecode(valuePart);
  const value = new Uint8Array(encodedValue.byteLength);
  value.set(encodedValue);
  const plain = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv },
    await aesKey(secret),
    value,
  );
  const phone = new TextDecoder("utf-8", { fatal: true }).decode(plain);
  if (!E164.test(phone)) throw new Error("invalid decrypted phone number");
  return phone;
}

function encryptionSecret(env: JobsEnv): string {
  const secret = String(env.PHONE_DATA_ENCRYPTION_SECRET || "").trim();
  if (!secret) throw new PhoneHttpError(503, "phone_unavailable", "phone data encryption is not configured");
  return secret;
}

async function boundedText(request: Request, maximum: number): Promise<string> {
  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > maximum)
    throw new PhoneHttpError(413, "request_too_large", "request body is too large");
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maximum)
        throw new PhoneHttpError(413, "request_too_large", "request body is too large");
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new PhoneHttpError(400, "invalid_encoding", "request body must be UTF-8");
  }
}

async function jsonBody(request: Request): Promise<Record<string, unknown>> {
  const raw = await boundedText(request, MAX_BODY_BYTES);
  if (!raw.trim()) throw new PhoneHttpError(400, "invalid_request", "JSON body is required");
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new PhoneHttpError(400, "invalid_request", "request body must be valid JSON");
  }
  const object = jsonObject(parsed);
  if (!object) throw new PhoneHttpError(400, "invalid_request", "request body must be an object");
  return object;
}

function twilioConfiguration(env: JobsEnv): { accountSid: string; authToken: string } {
  const accountSid = String(env.TWILIO_ACCOUNT_SID || "").trim();
  const authToken = String(env.TWILIO_AUTH_TOKEN || "").trim();
  if (!/^AC[A-Za-z0-9]{20,40}$/.test(accountSid) || !authToken)
    throw new PhoneHttpError(503, "twilio_unavailable", "Twilio is not configured");
  return { accountSid, authToken };
}

async function twilioRequest(
  env: JobsEnv,
  path: string,
  method: "GET" | "POST" | "DELETE",
  fields?: Record<string, string>,
): Promise<Record<string, unknown>> {
  const { accountSid, authToken } = twilioConfiguration(env);
  if (!path.startsWith("/2010-04-01/Accounts/") || path.includes(".."))
    throw new Error("invalid Twilio path");
  const headers = new Headers({
    authorization: `Basic ${base64Standard(new TextEncoder().encode(`${accountSid}:${authToken}`))}`,
  });
  let body: URLSearchParams | undefined;
  if (fields) {
    body = new URLSearchParams(fields);
    headers.set("content-type", "application/x-www-form-urlencoded");
  }
  let response: Response;
  try {
    response = await fetch(`https://api.twilio.com${path}`, {
      method,
      headers,
      body: method === "GET" ? undefined : body,
      signal: AbortSignal.timeout(10_000),
    });
  } catch {
    throw new TwilioError(503, null, "Twilio request failed");
  }
  const raw = await response.text();
  if (raw.length > 1_000_000) throw new TwilioError(502, null, "Twilio response is too large");
  let parsed: unknown = {};
  if (raw.trim()) {
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = {};
    }
  }
  const object = jsonObject(parsed) || {};
  if (!response.ok) {
    const code = typeof object.code === "number" || typeof object.code === "string" ? String(object.code) : null;
    const message = typeof object.message === "string" ? object.message.slice(0, 256) : "Twilio request failed";
    throw new TwilioError(response.status, code, message);
  }
  return object;
}

function twilioPath(env: JobsEnv, suffix: string): string {
  const { accountSid } = twilioConfiguration(env);
  return `/2010-04-01/Accounts/${encodeURIComponent(accountSid)}${suffix}`;
}

async function startVerification(env: JobsEnv, phone: string): Promise<{
  sid: string;
  callSid: string | null;
  validationCode: string;
  status: string;
}> {
  const result = await twilioRequest(
    env,
    twilioPath(env, "/OutgoingCallerIds.json"),
    "POST",
    { PhoneNumber: phone, FriendlyName: phone },
  );
  const sid = typeof result.sid === "string" ? result.sid : "";
  const validationCode = typeof result.validation_code === "string" ? result.validation_code : "";
  if (!SID.test(sid) || !validationCode) throw new TwilioError(502, null, "Twilio returned an invalid verification response");
  return {
    sid,
    callSid: typeof result.call_sid === "string" ? result.call_sid : null,
    validationCode,
    status: typeof result.status === "string" ? result.status : "pending",
  };
}

async function callerId(env: JobsEnv, phone: string): Promise<{ sid: string; friendlyName: string | null } | null> {
  const result = await twilioRequest(
    env,
    `${twilioPath(env, "/OutgoingCallerIds.json")}?PhoneNumber=${encodeURIComponent(phone)}&PageSize=1`,
    "GET",
  );
  const entries = Array.isArray(result.outgoing_caller_ids) ? result.outgoing_caller_ids : [];
  const first = jsonObject(entries[0]);
  const sid = typeof first?.sid === "string" ? first.sid : "";
  if (!sid) return null;
  return { sid, friendlyName: typeof first?.friendly_name === "string" ? first.friendly_name : null };
}

async function deleteCallerId(env: JobsEnv, sid: string): Promise<void> {
  if (!SID.test(sid)) throw new PhoneHttpError(500, "invalid_phone_state", "stored Twilio caller ID is invalid");
  try {
    await twilioRequest(env, twilioPath(env, `/OutgoingCallerIds/${encodeURIComponent(sid)}.json`), "DELETE");
  } catch (error) {
    if (error instanceof TwilioError && (error.status === 404 || error.code === "20404")) return;
    throw new PhoneHttpError(502, "twilio_unavailable", "Twilio caller ID deletion failed");
  }
}

async function checkCallerId(env: JobsEnv, phone: string): Promise<boolean> {
  return (await callerId(env, phone)) !== null;
}

function parseCountries(value: unknown): string[] {
  if (typeof value !== "string")
    throw new PhoneHttpError(503, "phone_unavailable", "phone call policy is malformed");
  try {
    const parsed = JSON.parse(value);
    if (!Array.isArray(parsed) || parsed.length > 64 || parsed.some((entry) => typeof entry !== "string" || !/^[A-Za-z]{2}$/.test(entry)))
      throw new Error("invalid country policy");
    return parsed.map((entry) => (entry as string).toUpperCase());
  } catch {
    throw new PhoneHttpError(503, "phone_unavailable", "phone call policy is malformed");
  }
}

function countriesForPhone(phone: string): string[] {
  return E164_COUNTRIES.find(([prefix]) => phone.startsWith(prefix))?.[1] || [];
}

async function accountState(env: JobsEnv, uid: string, generationRequired = true): Promise<number> {
  if (!validUid(uid)) throw new PhoneHttpError(401, "unauthorized", "invalid account");
  const now = nowSeconds();
  const fenced = await env.APP_DB.prepare(
    "SELECT uid FROM cf_account_deletion_intents WHERE uid = ? UNION ALL SELECT uid FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ? LIMIT 1",
  ).bind(uid, uid, now).first<{ uid?: unknown }>();
  if (fenced) throw new PhoneHttpError(409, "account_deletion_in_progress", "account deletion is in progress");
  const cutover = await env.APP_DB.prepare(
    "SELECT state, checkpoint_phase, destination_backend_bound, account_generation FROM cf_account_cutover WHERE uid = ?",
  ).bind(uid).first<Record<string, unknown>>();
  const generation = Number(cutover?.account_generation);
  if (
    generationRequired &&
    (!cutover || cutover.state !== "new" || cutover.checkpoint_phase !== "completed" || Number(cutover.destination_backend_bound) !== 1 || !Number.isSafeInteger(generation) || generation < 0)
  ) {
    throw new PhoneHttpError(503, "phone_unavailable", "account is not ready for Cloudflare phone services");
  }
  return Number.isSafeInteger(generation) && generation >= 0 ? generation : 0;
}

async function policy(env: JobsEnv): Promise<PolicyRow> {
  const row = await env.APP_DB.prepare("SELECT free_monthly_limit, free_max_duration_seconds, free_allowed_countries_json, paid_monthly_limit, paid_max_duration_seconds, paid_allowed_countries_json FROM cf_phone_call_policy WHERE id = 'default'").first<PolicyRow>();
  if (!row) throw new PhoneHttpError(503, "phone_unavailable", "phone call policy is not configured");
  return row;
}

async function accessSnapshot(env: JobsEnv, uid: string, generation: number): Promise<{
  paid: boolean;
  monthlyLimit: number | null;
  maxDurationSeconds: number | null;
  allowedCountries: string[];
  monthlyUsed: number;
  resetAt: number;
}> {
  const subscription = await env.APP_DB.prepare("SELECT plan, status FROM cf_user_subscriptions WHERE uid = ?").bind(uid).first<{ plan?: unknown; status?: unknown }>();
  const paid = subscription?.status === "active" && typeof subscription.plan === "string" && PAID_PLANS.has(subscription.plan);
  const config = await policy(env);
  const monthlyLimit = paid ? config.paid_monthly_limit : config.free_monthly_limit;
  const maxDurationSeconds = paid ? config.paid_max_duration_seconds : config.free_max_duration_seconds;
  const allowedCountries = parseCountries(paid ? config.paid_allowed_countries_json : config.free_allowed_countries_json);
  const now = new Date();
  const periodId = `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}`;
  const usedRow = await env.APP_DB.prepare("SELECT calls, account_generation FROM cf_phone_call_usage WHERE uid = ? AND period_id = ?").bind(uid, periodId).first<{ calls?: unknown; account_generation?: unknown }>();
  if (usedRow && Number(usedRow.account_generation) !== generation) throw new PhoneHttpError(409, "account_generation_conflict", "account generation changed");
  const nextMonth = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
  return {
    paid,
    monthlyLimit,
    maxDurationSeconds,
    allowedCountries,
    monthlyUsed: Math.max(0, Number(usedRow?.calls || 0)),
    resetAt: Math.floor(nextMonth.getTime() / 1000),
  };
}

function accessError(snapshot: Awaited<ReturnType<typeof accessSnapshot>>): PhoneHttpError | null {
  if (snapshot.paid || snapshot.monthlyLimit === null) return null;
  if (snapshot.monthlyLimit <= 0) return new PhoneHttpError(403, "phone_call_paid_plan_required", "Phone calls require a paid subscription");
  if (snapshot.monthlyUsed >= snapshot.monthlyLimit) return new PhoneHttpError(402, "phone_call_quota_exceeded", "monthly phone call limit reached");
  return null;
}

function destinationAllowed(snapshot: Awaited<ReturnType<typeof accessSnapshot>>, phone: string): boolean {
  if (snapshot.paid || snapshot.allowedCountries.length === 0) return true;
  const countries = countriesForPhone(phone);
  return countries.length > 0 && countries.some((country) => snapshot.allowedCountries.includes(country));
}

type CallAttemptClaim = "claimed" | "duplicate" | "busy";

/**
 * Claim a provider call id before reserving quota. Twilio can retry a signed
 * webhook while the first request is still running. A short pending lease
 * prevents the retry from spending another quota slot, and completed rows
 * make subsequent retries idempotent. Only a hash of CallId/CallSid is kept.
 */
async function claimCallAttempt(
  env: JobsEnv,
  uid: string,
  callId: string,
  generation: number,
): Promise<CallAttemptClaim> {
  const hash = await sha256Hex(callId);
  const now = nowSeconds();
  const expiresAt = now + CALL_ATTEMPT_TTL_SECONDS;
  const result = await env.APP_DB.prepare(
    "INSERT INTO cf_phone_call_attempts (uid, call_id_hash, account_generation, status, expires_at, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?) ON CONFLICT(uid, call_id_hash) DO UPDATE SET account_generation = excluded.account_generation, status = 'pending', expires_at = excluded.expires_at, updated_at = excluded.updated_at WHERE cf_phone_call_attempts.account_generation <> excluded.account_generation OR cf_phone_call_attempts.expires_at <= excluded.updated_at",
  ).bind(uid, hash, generation, expiresAt, now, now).run();
  if (Number(result.meta?.changes || 0) === 1) return "claimed";
  const existing = await env.APP_DB.prepare(
    "SELECT account_generation, status, expires_at FROM cf_phone_call_attempts WHERE uid = ? AND call_id_hash = ?",
  ).bind(uid, hash).first<{ account_generation?: unknown; status?: unknown; expires_at?: unknown }>();
  if (!existing || Number(existing.account_generation) !== generation) {
    throw new PhoneHttpError(503, "phone_unavailable", "phone call idempotency state is unavailable");
  }
  if (existing.status === "completed") return "duplicate";
  return Number(existing.expires_at) > now ? "busy" : "claimed";
}

async function completeCallAttempt(env: JobsEnv, uid: string, callId: string, generation: number): Promise<void> {
  const hash = await sha256Hex(callId);
  await env.APP_DB.prepare(
    "UPDATE cf_phone_call_attempts SET status = 'completed', expires_at = ?, updated_at = ? WHERE uid = ? AND call_id_hash = ? AND account_generation = ? AND status = 'pending'",
  ).bind(nowSeconds() + CALL_ATTEMPT_TTL_SECONDS, nowSeconds(), uid, hash, generation).run();
}

async function releaseCallAttempt(env: JobsEnv, uid: string, callId: string, generation: number): Promise<void> {
  const hash = await sha256Hex(callId);
  await env.APP_DB.prepare(
    "DELETE FROM cf_phone_call_attempts WHERE uid = ? AND call_id_hash = ? AND account_generation = ? AND status = 'pending'",
  ).bind(uid, hash, generation).run();
}

async function reserveQuota(env: JobsEnv, uid: string, generation: number, snapshot: Awaited<ReturnType<typeof accessSnapshot>>): Promise<boolean> {
  if (snapshot.paid || snapshot.monthlyLimit === null) return true;
  if (snapshot.monthlyLimit <= 0) return false;
  const now = new Date();
  const periodId = `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}`;
  const result = await env.APP_DB.prepare(
    "INSERT INTO cf_phone_call_usage (uid, period_id, calls, account_generation, updated_at) VALUES (?, ?, 1, ?, ?) ON CONFLICT(uid, period_id) DO UPDATE SET calls = cf_phone_call_usage.calls + 1, updated_at = excluded.updated_at WHERE cf_phone_call_usage.calls < ? AND cf_phone_call_usage.account_generation = ?",
  ).bind(uid, periodId, generation, Math.floor(Date.now() / 1000), snapshot.monthlyLimit, generation).run();
  return Number(result.meta?.changes || 0) === 1;
}

function responseForPhone(row: PhoneRow, phone: string): Record<string, unknown> {
  return {
    id: row.id,
    phone_number: phone,
    friendly_name: row.friendly_name,
    verified_at: new Date(row.verified_at * 1000).toISOString(),
    is_primary: row.is_primary === 1,
  };
}

function xmlEscape(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&apos;");
}

function say(message: string): Response {
  return new Response(`<?xml version="1.0" encoding="UTF-8"?><Response><Say>${xmlEscape(message)}</Say></Response>`, { status: 200, headers: { "content-type": "text/xml; charset=UTF-8", "cache-control": "no-store" } });
}

function dialResponse(caller: string, destination: string, maxDuration: number | null): Response {
  const timeLimit = maxDuration && maxDuration > 0 ? ` timeLimit="${Math.floor(maxDuration)}"` : "";
  return new Response(`<?xml version="1.0" encoding="UTF-8"?><Response><Dial callerId="${xmlEscape(caller)}"${timeLimit}><Number>${xmlEscape(destination)}</Number></Dial></Response>`, { status: 200, headers: { "content-type": "text/xml; charset=UTF-8", "cache-control": "no-store" } });
}

export async function validateTwilioSignature(url: string, params: URLSearchParams, signature: string, authToken: string): Promise<boolean> {
  if (!signature || !authToken) return false;
  return constantTimeEqual(await twilioSignature(url, params, authToken), signature);
}

// Standard base64 HMAC helper kept separate so validation never accidentally
// compares an URL-safe token with Twilio's wire header.
async function twilioSignature(url: string, params: URLSearchParams, authToken: string): Promise<string> {
  const entries = [...params.entries()].sort(([left], [right]) => left.localeCompare(right));
  const payload = `${url}${entries.map(([key, value]) => `${key}${value}`).join("")}`;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(authToken), { name: "HMAC", hash: "SHA-1" }, false, ["sign"]);
  const bytes = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(payload)));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function publicWebhookUrl(env: JobsEnv, request: Request): string {
  const base = String(env.PUBLIC_API_BASE_URL || "").trim().replace(/\/$/, "");
  const incoming = new URL(request.url);
  return `${base || incoming.origin}${incoming.pathname}${incoming.search}`;
}

async function initiateVerification(c: JobsContext, context: AuthContext): Promise<Response> {
  const body = await jsonBody(c.req.raw);
  const phone = normalizePhone(body.phone_number);
  const generation = await accountState(c.env, context.uid);
  const secret = encryptionSecret(c.env);
  const hash = await sha256Hex(phone);
  const now = nowSeconds();
  const existingNumber = await c.env.APP_DB.prepare("SELECT id FROM cf_phone_numbers WHERE uid = ? AND phone_number_hash = ?").bind(context.uid, hash).first<{ id?: unknown }>();
  if (existingNumber) throw new PhoneHttpError(409, "phone_already_verified", "Phone number already verified");
  const existing = await c.env.APP_DB.prepare("SELECT verification_id, uid, status, expires_at, attempts FROM cf_phone_verifications WHERE phone_number_hash = ?").bind(hash).first<VerificationRow>();
  if (existing?.status === "pending" && existing.expires_at > now) throw new PhoneHttpError(409, "verification_in_progress", "A verification call is already in progress for this number");
  if (existing) await c.env.APP_DB.prepare("DELETE FROM cf_phone_verifications WHERE verification_id = ? AND uid = ?").bind(existing.verification_id, existing.uid).run();
  const verificationId = crypto.randomUUID();
  const ciphertext = await encryptPhone(secret, phone);
  await c.env.APP_DB.prepare("INSERT INTO cf_phone_verifications (verification_id, uid, phone_number_hash, phone_number_ciphertext, status, account_generation, attempts, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, 0, ?, ?, ?)").bind(verificationId, context.uid, hash, ciphertext, generation, now + VERIFICATION_TTL_SECONDS, now, now).run();
  let result: Awaited<ReturnType<typeof startVerification>>;
  try {
    result = await startVerification(c.env, phone);
  } catch (error) {
    const code = error instanceof TwilioError && error.code === "21450" ? "verification_in_progress" : "twilio_unavailable";
    await c.env.APP_DB.prepare("UPDATE cf_phone_verifications SET status = 'failed', attempts = attempts + 1, last_error = ?, updated_at = ? WHERE verification_id = ? AND uid = ?").bind(code, nowSeconds(), verificationId, context.uid).run();
    if (error instanceof TwilioError && error.code === "21450") throw new PhoneHttpError(409, code, "A verification call is already in progress for this number");
    throw new PhoneHttpError(502, code, "Failed to start phone verification");
  }
  await c.env.APP_DB.prepare("UPDATE cf_phone_verifications SET twilio_validation_sid = ?, attempts = attempts + 1, updated_at = ? WHERE verification_id = ? AND uid = ? AND status = 'pending'").bind(result.sid, nowSeconds(), verificationId, context.uid).run();
  return c.json({ verification_sid: result.callSid || result.sid, phone_number: phone, validation_code: result.validationCode, status: result.status }, 200);
}

async function checkVerification(c: JobsContext, context: AuthContext): Promise<Response> {
  const body = await jsonBody(c.req.raw);
  const phone = normalizePhone(body.phone_number);
  const generation = await accountState(c.env, context.uid);
  const secret = encryptionSecret(c.env);
  const hash = await sha256Hex(phone);
  const existingNumber = await c.env.APP_DB.prepare("SELECT id, uid, phone_number_hash, phone_number_ciphertext, friendly_name, twilio_sid, verified_at, is_primary, account_generation FROM cf_phone_numbers WHERE uid = ? AND phone_number_hash = ?").bind(context.uid, hash).first<PhoneRow>();
  if (existingNumber) return c.json({ verified: true, phone_number_id: existingNumber.id }, 200);
  const pending = await c.env.APP_DB.prepare("SELECT verification_id, uid, phone_number_hash, phone_number_ciphertext, twilio_validation_sid, status, account_generation, attempts, expires_at FROM cf_phone_verifications WHERE phone_number_hash = ?").bind(hash).first<VerificationRow>();
  const now = nowSeconds();
  if (!pending || pending.uid !== context.uid || pending.account_generation !== generation || pending.status !== "pending") return c.json({ verified: false }, 200);
  if (pending.expires_at <= now) {
    await c.env.APP_DB.prepare("UPDATE cf_phone_verifications SET status = 'expired', updated_at = ? WHERE verification_id = ? AND uid = ? AND status = 'pending'").bind(now, pending.verification_id, context.uid).run();
    return c.json({ verified: false }, 200);
  }
  try {
    if (!(await checkCallerId(c.env, phone))) return c.json({ verified: false }, 200);
    const caller = await callerId(c.env, phone);
    if (!caller) return c.json({ verified: false }, 200);
    const existingCount = await c.env.APP_DB.prepare("SELECT COUNT(*) AS count FROM cf_phone_numbers WHERE uid = ? AND account_generation = ?").bind(context.uid, generation).first<{ count?: unknown }>();
    const id = crypto.randomUUID();
    const timestamp = nowSeconds();
    const insert = await c.env.APP_DB.prepare("INSERT INTO cf_phone_numbers (uid, id, phone_number_hash, phone_number_ciphertext, friendly_name, twilio_sid, verified_at, is_primary, account_generation, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)").bind(context.uid, id, hash, await encryptPhone(secret, phone), caller.friendlyName, caller.sid, timestamp, Number(existingCount?.count || 0) === 0 ? 1 : 0, generation, timestamp, timestamp).run();
    if (Number(insert.meta?.changes || 0) !== 1) return c.json({ verified: false }, 200);
    await c.env.APP_DB.prepare("DELETE FROM cf_phone_verifications WHERE verification_id = ? AND uid = ?").bind(pending.verification_id, context.uid).run();
    return c.json({ verified: true, phone_number_id: id }, 200);
  } catch (error) {
    if (error instanceof TwilioError) throw new PhoneHttpError(502, "twilio_unavailable", "Failed to check phone verification");
    throw error;
  }
}

async function listNumbers(c: JobsContext, context: AuthContext): Promise<Response> {
  const generation = await accountState(c.env, context.uid);
  const secret = encryptionSecret(c.env);
  const result = await c.env.APP_DB.prepare("SELECT uid, id, phone_number_hash, phone_number_ciphertext, friendly_name, twilio_sid, verified_at, is_primary, account_generation FROM cf_phone_numbers WHERE uid = ? AND account_generation = ? ORDER BY is_primary DESC, verified_at ASC").bind(context.uid, generation).all<PhoneRow>();
  const numbers = [];
  for (const row of result.results || []) numbers.push(responseForPhone(row, await decryptPhone(secret, row.phone_number_ciphertext)));
  return c.json({ numbers }, 200);
}

async function removeNumber(c: JobsContext, context: AuthContext): Promise<Response> {
  const generation = await accountState(c.env, context.uid);
  const id = c.req.param("phoneNumberId");
  if (!id || id.length > 128 || id.includes("/")) throw new PhoneHttpError(400, "invalid_phone_number_id", "phone number id is invalid");
  const row = await c.env.APP_DB.prepare("SELECT uid, id, phone_number_hash, phone_number_ciphertext, friendly_name, twilio_sid, verified_at, is_primary, account_generation FROM cf_phone_numbers WHERE uid = ? AND id = ? AND account_generation = ?").bind(context.uid, id, generation).first<PhoneRow>();
  if (!row) throw new PhoneHttpError(404, "phone_number_not_found", "Phone number not found");
  if (row.twilio_sid) await deleteCallerId(c.env, row.twilio_sid);
  await c.env.APP_DB.prepare("DELETE FROM cf_phone_numbers WHERE uid = ? AND id = ? AND account_generation = ?").bind(context.uid, id, generation).run();
  return c.json({ success: true }, 200);
}

async function createToken(c: JobsContext, context: AuthContext): Promise<Response> {
  const generation = await accountState(c.env, context.uid);
  const primary = await c.env.APP_DB.prepare("SELECT id FROM cf_phone_numbers WHERE uid = ? AND account_generation = ? ORDER BY is_primary DESC, verified_at ASC LIMIT 1").bind(context.uid, generation).first<{ id?: unknown }>();
  if (!primary) throw new PhoneHttpError(400, "phone_number_required", "No verified phone number found. Verify a number first.");
  const accountSid = String(c.env.TWILIO_ACCOUNT_SID || "").trim();
  const apiKeySid = String(c.env.TWILIO_API_KEY_SID || "").trim();
  const apiKeySecret = String(c.env.TWILIO_API_KEY_SECRET || "").trim();
  const applicationSid = String(c.env.TWILIO_TWIML_APP_SID || "").trim();
  if (!/^AC[A-Za-z0-9]{20,40}$/.test(accountSid) || !/^SK[A-Za-z0-9]{20,40}$/.test(apiKeySid) || !apiKeySecret || !/^AP[A-Za-z0-9]{20,40}$/.test(applicationSid)) throw new PhoneHttpError(503, "twilio_unavailable", "Twilio Voice token is not configured");
  if (!validUid(context.uid)) throw new PhoneHttpError(401, "unauthorized", "invalid account");
  const issuedAt = nowSeconds();
  const header = base64Url(new TextEncoder().encode(JSON.stringify({ typ: "JWT", alg: "HS256" })));
  const payload = base64Url(new TextEncoder().encode(JSON.stringify({
    jti: `${apiKeySid}-${issuedAt}`,
    sub: accountSid,
    grants: { identity: context.uid, voice: { outgoing: { application_sid: applicationSid }, incoming: { allow: false } } },
    iat: issuedAt,
    exp: issuedAt + TOKEN_TTL_SECONDS,
    iss: apiKeySid,
  })));
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(apiKeySecret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const signature = base64Url(new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${header}.${payload}`))));
  return c.json({ access_token: `${header}.${payload}.${signature}`, ttl: TOKEN_TTL_SECONDS, identity: context.uid }, 200);
}

async function twiml(c: JobsContext): Promise<Response> {
  const body = await boundedText(c.req.raw, MAX_TWIML_BODY_BYTES);
  const params = new URLSearchParams(body);
  const config = twilioConfiguration(c.env);
  const signature = c.req.header("x-twilio-signature") || "";
  const expected = await twilioSignature(publicWebhookUrl(c.env, c.req.raw), params, config.authToken);
  if (!constantTimeEqual(expected, signature)) throw new PhoneHttpError(403, "invalid_twilio_signature", "Invalid Twilio signature");
  const rawDestination = params.get("To") || "";
  let to: string;
  try {
    to = normalizePhone(rawDestination);
  } catch (error) {
    if (error instanceof PhoneHttpError && error.code === "invalid_phone_number") {
      return say(rawDestination ? "Invalid destination number format. Goodbye." : "No destination number provided. Goodbye.");
    }
    throw error;
  }
  const callerIdentity = params.get("From") || "";
  const uid = callerIdentity.startsWith("client:") ? callerIdentity.slice("client:".length) : callerIdentity;
  if (!validUid(uid)) return say("No verified caller ID found. Please verify a phone number first.");
  let generation: number;
  try {
    generation = await accountState(c.env, uid);
  } catch (error) {
    if (error instanceof PhoneHttpError && (error.status === 409 || error.status === 503)) return say("Phone calling is temporarily unavailable. Goodbye.");
    throw error;
  }
  const secret = encryptionSecret(c.env);
  const primary = await c.env.APP_DB.prepare("SELECT uid, id, phone_number_hash, phone_number_ciphertext, friendly_name, twilio_sid, verified_at, is_primary, account_generation FROM cf_phone_numbers WHERE uid = ? AND account_generation = ? ORDER BY is_primary DESC, verified_at ASC LIMIT 1").bind(uid, generation).first<PhoneRow>();
  if (!primary) return say("No verified caller ID found. Please verify a phone number first.");
  const caller = await decryptPhone(secret, primary.phone_number_ciphertext);
  const snapshot = await accessSnapshot(c.env, uid, generation);
  if (accessError(snapshot)) return say("Monthly phone call limit reached. Goodbye.");
  if (!destinationAllowed(snapshot, to)) return say("This destination is not available on your plan. Goodbye.");
  try {
    if (!(await checkCallerId(c.env, caller))) return say("Your caller ID is not verified. Please re-verify your phone number.");
    const callId = (params.get("CallId") || params.get("CallSid") || "").trim();
    if (callId.length > 256) return say("Invalid call identifier. Goodbye.");
    const claim = callId ? await claimCallAttempt(c.env, uid, callId, generation) : "claimed";
    if (claim === "duplicate") return dialResponse(caller, to, snapshot.maxDurationSeconds);
    if (claim === "busy") return say("Phone calling is temporarily unavailable. Goodbye.");
    if (!(await reserveQuota(c.env, uid, generation, snapshot))) {
      if (callId) await releaseCallAttempt(c.env, uid, callId, generation);
      return say("Monthly phone call limit reached. Goodbye.");
    }
    if (callId) await completeCallAttempt(c.env, uid, callId, generation);
  } catch (error) {
    if (error instanceof TwilioError) return say("Phone calling is temporarily unavailable. Goodbye.");
    throw error;
  }
  return dialResponse(caller, to, snapshot.maxDurationSeconds);
}

function errorResponse(c: JobsContext, error: unknown): Response {
  if (error instanceof PhoneHttpError) return c.json({ error: error.code, detail: error.message }, error.status as 400);
  return c.json({ error: "phone_unavailable", detail: "Phone service is temporarily unavailable" }, 503);
}

export function registerPhoneTwilioRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: (c: JobsContext) => Promise<AuthContext | null>,
): void {
  const authenticated = async (c: JobsContext, operation: (context: AuthContext) => Promise<Response>) => {
    const context = await requestContext(c);
    if (!context || !validUid(context.uid)) return c.json({ error: "unauthorized" }, 401);
    try {
      return await operation(context);
    } catch (error) {
      return errorResponse(c, error);
    }
  };
  app.get("/v1/phone/numbers", (c) => authenticated(c, (context) => listNumbers(c, context)));
  app.delete("/v1/phone/numbers/:phoneNumberId", (c) => authenticated(c, (context) => removeNumber(c, context)));
  app.post("/v1/phone/numbers/verify", (c) => authenticated(c, (context) => initiateVerification(c, context)));
  app.post("/v1/phone/numbers/verify/check", (c) => authenticated(c, (context) => checkVerification(c, context)));
  app.post("/v1/phone/token", (c) => authenticated(c, (context) => createToken(c, context)));
  app.post("/v1/phone/twiml", async (c) => {
    try {
      return await twiml(c);
    } catch (error) {
      return errorResponse(c, error);
    }
  });
}
