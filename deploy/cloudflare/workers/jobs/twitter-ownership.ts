import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobsEnv } from "./env";
import {
  fetchTwitterProfile,
  type TwitterProfilePayload,
} from "./twitter-profile";

const MAX_REQUEST_TEXT = 256;
const MAX_HANDLE_LENGTH = 128;
const MAX_PERSONA_ID_LENGTH = 256;
const MAX_PROVIDER_RESPONSE_BYTES = 512_000;
const MAX_TWEET_TEXT_LENGTH = 20_000;
const MAX_TIMELINE_ITEMS = 100;
const MAX_ERROR_LENGTH = 512;
const RAPID_API_HOST_PATTERN = /^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$/;
const HANDLE_PATTERN = /^[a-zA-Z0-9_.-]+$/;
const VERIFICATION_PHRASE = "Verifying my clone";

type OwnershipContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (
  c: OwnershipContext,
) => Promise<SignedAuthContext | null>;
type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type TwitterOwnershipDependencies = Readonly<{
  fetchImpl?: FetchLike;
  now?: () => number;
}>;

type OwnershipResult = Readonly<{
  tweet: string;
  tweetId: string | null;
  verified: boolean;
  providerRequestFingerprint: string;
  providerResponseFingerprint: string | null;
  personaId?: string;
}>;

type TransactionRow = {
  uid: string;
  transaction_id: string;
  account_generation: number;
  username: string;
  handle: string;
  persona_id: string | null;
  status: "pending" | "verified" | "unverified" | "conflict" | "failed";
  verified: number;
  tweet_id: string | null;
  result_json: string;
  attempts: number;
  last_error: string | null;
  provider_response_fingerprint: string | null;
};

type ClaimRow = {
  handle: string;
  uid: string;
  account_generation: number;
  transaction_id: string;
  username: string;
  tweet_id: string;
};

type PersonaCatalogRow = {
  id: string;
  owner_uid: string | null;
  data_json: string;
};

class TwitterOwnershipError extends Error {
  constructor(
    readonly status: 400 | 403 | 404 | 409 | 422 | 502 | 503,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

class TwitterOwnershipProviderError extends Error {
  constructor(readonly code: "provider_not_configured" | "provider_unavailable") {
    super(code);
  }
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function nowSeconds(dependencies: TwitterOwnershipDependencies) {
  return dependencies.now?.() ?? Math.floor(Date.now() / 1_000);
}

function utf8Length(value: string) {
  return new TextEncoder().encode(value).byteLength;
}

function boundedText(value: unknown, maximum: number): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  return text && utf8Length(text) <= maximum ? text : null;
}

function normalizeHandle(value: string | undefined): string | null {
  const handle = (value || "").trim().replace(/^@/, "");
  if (
    !handle ||
    handle.length > MAX_HANDLE_LENGTH ||
    !HANDLE_PATTERN.test(handle)
  ) {
    return null;
  }
  return handle;
}

function normalizeUsername(value: string | undefined): string | null {
  const username = (value || "").trim();
  if (!username || utf8Length(username) > MAX_REQUEST_TEXT) return null;
  return username;
}

function normalizePersonaId(value: string | undefined): string | null {
  if (!value) return null;
  const personaId = value.trim();
  if (
    !personaId ||
    personaId.length > MAX_PERSONA_ID_LENGTH ||
    personaId.includes("/") ||
    personaId.includes("\\")
  ) {
    return null;
  }
  return personaId;
}

function rapidApiConfiguration(env: JobsEnv) {
  const host = env.RAPID_API_HOST?.trim().toLowerCase();
  const key = env.RAPID_API_KEY?.trim();
  if (!host || !key || !RAPID_API_HOST_PATTERN.test(host)) return null;
  return { host, key };
}

async function sha256Hex(value: string | ArrayBuffer) {
  const bytes =
    typeof value === "string" ? new TextEncoder().encode(value) : value;
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function providerErrorResponse(error: unknown): TwitterOwnershipError {
  if (error instanceof TwitterOwnershipProviderError) {
    return new TwitterOwnershipError(
      error.code === "provider_not_configured" ? 503 : 502,
      error.code,
      error.code === "provider_not_configured"
        ? "Twitter ownership provider is not configured"
        : "Twitter ownership provider is unavailable",
    );
  }
  return new TwitterOwnershipError(
    502,
    "provider_unavailable",
    "Twitter ownership provider is unavailable",
  );
}

async function readProviderTimeline(
  env: JobsEnv,
  handle: string,
  dependencies: TwitterOwnershipDependencies,
) {
  const configuration = rapidApiConfiguration(env);
  if (!configuration)
    throw new TwitterOwnershipProviderError("provider_not_configured");
  const url = new URL(`https://${configuration.host}/timeline.php`);
  url.searchParams.set("screenname", handle);
  let response: Response;
  try {
    response = await (dependencies.fetchImpl || fetch)(url, {
      headers: {
        "x-rapidapi-key": configuration.key,
        "x-rapidapi-host": configuration.host,
        accept: "application/json",
      },
      signal: AbortSignal.timeout(20_000),
    });
  } catch {
    throw new TwitterOwnershipProviderError("provider_unavailable");
  }
  let bytes: ArrayBuffer;
  try {
    const declared = Number(response.headers.get("content-length") || 0);
    if (
      Number.isFinite(declared) &&
      declared > MAX_PROVIDER_RESPONSE_BYTES
    ) {
      throw new TwitterOwnershipProviderError("provider_unavailable");
    }
    bytes = await response.arrayBuffer();
  } catch (error) {
    if (error instanceof TwitterOwnershipProviderError) throw error;
    throw new TwitterOwnershipProviderError("provider_unavailable");
  }
  if (bytes.byteLength > MAX_PROVIDER_RESPONSE_BYTES || !response.ok) {
    throw new TwitterOwnershipProviderError("provider_unavailable");
  }
  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new TwitterOwnershipProviderError("provider_unavailable");
  }
  const object = objectValue(payload);
  const timeline = object?.timeline;
  if (!object || object.status === "error" || !Array.isArray(timeline)) {
    throw new TwitterOwnershipProviderError("provider_unavailable");
  }
  if (timeline.length > MAX_TIMELINE_ITEMS) {
    throw new TwitterOwnershipProviderError("provider_unavailable");
  }
  const providerResponseFingerprint = await sha256Hex(bytes);
  return { timeline, providerResponseFingerprint };
}

/**
 * Verify only the legacy phrase in the latest RapidAPI timeline item.
 *
 * This helper deliberately has no Firebase or Persona dependency.  Its
 * output is evidence for the dormant migration transaction, not proof that a
 * caller may mutate a Persona or claim a Firebase identity.
 */
export async function verifyLatestTwitterTweet(
  env: JobsEnv,
  username: string,
  handle: string,
  dependencies: TwitterOwnershipDependencies = {},
): Promise<OwnershipResult> {
  const providerRequestFingerprint = await sha256Hex(
    `rapidapi-timeline:v1\0${handle}`,
  );
  const { timeline, providerResponseFingerprint } =
    await readProviderTimeline(env, handle, dependencies);
  if (!timeline.length) {
    return {
      tweet: "",
      tweetId: null,
      verified: false,
      providerRequestFingerprint,
      providerResponseFingerprint,
    };
  }
  const latest = objectValue(timeline[0]);
  const tweet = boundedText(latest?.text, MAX_TWEET_TEXT_LENGTH);
  const tweetId = boundedText(
    latest?.tweet_id ?? latest?.id,
    MAX_PERSONA_ID_LENGTH,
  );
  if (!tweet || !tweetId) {
    throw new TwitterOwnershipProviderError("provider_unavailable");
  }
  return {
    tweet,
    tweetId,
    verified: tweet.includes(`${VERIFICATION_PHRASE}(${username})`),
    providerRequestFingerprint,
    providerResponseFingerprint,
  };
}

function validUid(value: string) {
  return value.length > 0 && value.length <= 256 && !value.includes("/");
}

async function accountState(env: JobsEnv, uid: string) {
  const cutover = await env.APP_DB.prepare(
    "SELECT account_generation FROM cf_account_cutover WHERE uid = ? LIMIT 1",
  )
    .bind(uid)
    .first<{ account_generation?: unknown }>();
  const generation = Number(cutover?.account_generation);
  if (!cutover || !Number.isSafeInteger(generation) || generation < 0) {
    throw new TwitterOwnershipError(
      503,
      "metadata_unavailable",
      "account migration metadata is unavailable",
    );
  }
  const fenced = await env.APP_DB.prepare(
    "SELECT 1 AS fenced FROM cf_account_deletion_intents WHERE uid = ? LIMIT 1",
  )
    .bind(uid)
    .first<{ fenced?: unknown }>();
  const tombstone = await env.APP_DB.prepare(
    "SELECT 1 AS fenced FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > unixepoch() LIMIT 1",
  )
    .bind(uid)
    .first<{ fenced?: unknown }>();
  if (fenced || tombstone) {
    throw new TwitterOwnershipError(
      409,
      "account_deletion_fenced",
      "account data plane is fenced",
    );
  }
  return generation;
}

function publicResult(
  row: TransactionRow,
  claimStatus: string | null = null,
  legacyWire = false,
) {
  let result: { tweet?: unknown; verified?: unknown; persona_id?: unknown } = {};
  try {
    result = JSON.parse(row.result_json) as {
      tweet?: unknown;
      verified?: unknown;
      persona_id?: unknown;
    };
  } catch {
    // A malformed result is never returned as a successful proof.
  }
  const personaId = typeof result.persona_id === "string" ? result.persona_id : undefined;
  if (legacyWire) {
    return {
      tweet: typeof result.tweet === "string" ? result.tweet : "",
      verified: row.status === "verified" && row.verified === 1,
      ...(personaId ? { persona_id: personaId } : {}),
    };
  }
  return {
    transaction_id: row.transaction_id,
    tweet: typeof result.tweet === "string" ? result.tweet : "",
    verified: row.status === "verified" && row.verified === 1,
    status: row.status,
    claim_status: claimStatus,
    ...(personaId ? { persona_id: personaId } : {}),
  };
}

async function readTransaction(
  env: JobsEnv,
  uid: string,
  requestFingerprint: string,
) {
  return env.APP_DB.prepare(
    "SELECT uid, transaction_id, account_generation, username, handle, persona_id, status, verified, tweet_id, result_json, attempts, last_error, provider_response_fingerprint FROM cf_twitter_ownership_transactions WHERE uid = ? AND request_fingerprint = ? LIMIT 1",
  )
    .bind(uid, requestFingerprint)
    .first<TransactionRow>();
}

function catalogPayload(raw: string, personaId: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new TwitterOwnershipError(503, "persona_unavailable", "Persona is unavailable");
  }
  const object = objectValue(parsed);
  if (!object || object.id !== undefined && object.id !== personaId) {
    throw new TwitterOwnershipError(503, "persona_unavailable", "Persona is unavailable");
  }
  if (!Array.isArray(object.capabilities) || !object.capabilities.includes("persona")) {
    throw new TwitterOwnershipError(404, "persona_not_found", "Persona not found");
  }
  return object;
}

function catalogString(value: unknown, maximum: number): string {
  return typeof value === "string" ? value.trim().slice(0, maximum) : "";
}

function catalogWriteError(error: unknown): TwitterOwnershipError {
  if (error instanceof Error && error.message.includes("account deletion fence")) {
    return new TwitterOwnershipError(
      409,
      "account_deletion_fenced",
      "account data plane is fenced",
    );
  }
  return new TwitterOwnershipError(409, "persona_changed", "Persona changed during update");
}

/**
 * Apply the smallest Cloudflare-owned Persona projection needed by the exact
 * Twitter verification contract.  This intentionally writes only D1 catalog
 * metadata; legacy prompt generation, memory extraction, and public-cache
 * invalidation remain separate migration gates.
 */
async function attachTwitterPersona(
  env: JobsEnv,
  uid: string,
  username: string,
  handle: string,
  profile: TwitterProfilePayload,
  requestedPersonaId: string | null,
  now: number,
) {
  let row: PersonaCatalogRow | null = null;
  if (requestedPersonaId) {
    row = await env.APP_DB.prepare(
      "SELECT id, owner_uid, data_json FROM cf_app_catalog WHERE id = ? AND disabled = 0 LIMIT 1",
    )
      .bind(requestedPersonaId)
      .first<PersonaCatalogRow>();
    if (!row) {
      throw new TwitterOwnershipError(404, "persona_not_found", "Persona not found");
    }
    if (row.owner_uid !== uid) {
      throw new TwitterOwnershipError(403, "persona_owner_mismatch", "Persona owner does not match");
    }
  } else {
    row = await env.APP_DB.prepare(
      `SELECT id, owner_uid, data_json FROM cf_app_catalog
         WHERE owner_uid = ? AND disabled = 0
           AND json_extract(data_json, '$.username') = ?
         ORDER BY updated_at DESC, id DESC LIMIT 1`,
    )
      .bind(uid, username)
      .first<PersonaCatalogRow>();
    if (!row) {
      const conflicting = await env.APP_DB.prepare(
        `SELECT id FROM cf_app_catalog WHERE disabled = 0
           AND json_extract(data_json, '$.username') = ? LIMIT 1`,
      )
        .bind(username)
        .first<{ id?: string }>();
      if (conflicting) {
        throw new TwitterOwnershipError(409, "persona_username_taken", "Persona username is already taken");
      }
    }
  }

  const personaId = row?.id || `twitter-persona-${crypto.randomUUID()}`;
  const existing = row ? catalogPayload(row.data_json, personaId) : {};
  const priorAccounts = Array.isArray(existing.connected_accounts)
    ? existing.connected_accounts.filter((value): value is string => typeof value === "string")
    : [];
  const priorTwitter = objectValue(existing.twitter) || {};
  const profileHandle = catalogString(profile.profile, MAX_HANDLE_LENGTH) || handle;
  const payload: Record<string, unknown> = {
    ...existing,
    id: personaId,
    uid,
    owner_uid: uid,
    name: catalogString(profile.name, 512) || username,
    author: catalogString(profile.name, 512) || username,
    username,
    description: catalogString(profile.desc, 4_000),
    image: catalogString(profile.avatar, 2_048),
    category: existing.category || "personality-emulation",
    capabilities: ["persona"],
    connected_accounts: [...new Set([...priorAccounts, "twitter"])],
    approved: true,
    status: "approved",
    private: existing.private === true,
    twitter: {
      ...priorTwitter,
      username: profileHandle,
      avatar: catalogString(profile.avatar, 2_048),
      connected_at: new Date(now * 1_000).toISOString(),
    },
    updated_at: new Date(now * 1_000).toISOString(),
  };
  if (!row) payload.created_at = new Date(now * 1_000).toISOString();
  const encoded = JSON.stringify(payload);
  if (utf8Length(encoded) > 500_000) {
    throw new TwitterOwnershipError(503, "persona_unavailable", "Persona is unavailable");
  }
  if (row) {
    let update: { meta?: { changes?: number } };
    try {
      update = await env.APP_DB.prepare(
        `UPDATE cf_app_catalog SET data_json = ?, updated_at = ?
           WHERE id = ? AND owner_uid = ? AND data_json = ?`,
      )
        .bind(encoded, now, personaId, uid, row.data_json)
        .run();
    } catch (error) {
      throw catalogWriteError(error);
    }
    if (Number(update.meta?.changes || 0) !== 1) {
      throw new TwitterOwnershipError(409, "persona_changed", "Persona changed during update");
    }
  } else {
    try {
      await env.APP_DB.prepare(
        `INSERT INTO cf_app_catalog
           (id, approved, status, disabled, is_popular, installs, rating_avg,
            rating_count, data_json, updated_at, owner_uid)
         VALUES (?, 1, 'approved', 0, 0, 0, NULL, 0, ?, ?, ?)`,
      )
        .bind(personaId, encoded, now, uid)
        .run();
    } catch (error) {
      throw catalogWriteError(error);
    }
  }
  return personaId;
}

async function markFailed(
  env: JobsEnv,
  uid: string,
  transactionId: string,
  now: number,
  code: string,
) {
  await env.APP_DB.prepare(
    "UPDATE cf_twitter_ownership_transactions SET status = 'failed', verified = 0, last_error = ?, updated_at = ? WHERE uid = ? AND transaction_id = ? AND status IN ('pending', 'failed')",
  )
    .bind(code.slice(0, MAX_ERROR_LENGTH), now, uid, transactionId)
    .run();
}

async function claimHandle(
  env: JobsEnv,
  uid: string,
  generation: number,
  transactionId: string,
  username: string,
  handle: string,
  tweetId: string,
  now: number,
) {
  let existing = await env.APP_DB.prepare(
    "SELECT handle, uid, account_generation, transaction_id, username, tweet_id FROM cf_twitter_ownership_claims WHERE handle = ? LIMIT 1",
  )
    .bind(handle)
    .first<ClaimRow>();
  let inserted = false;
  if (existing && existing.uid !== uid) {
    return { conflict: true, status: "conflict" } as const;
  }
  if (!existing) {
    try {
      await env.APP_DB.prepare(
        "INSERT OR IGNORE INTO cf_twitter_ownership_claims (handle, uid, account_generation, transaction_id, username, tweet_id, claimed_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
      )
        .bind(handle, uid, generation, transactionId, username, tweetId, now, now)
        .run();
      inserted = true;
    } catch {
      throw new TwitterOwnershipError(
        503,
        "transaction_unavailable",
        "Twitter ownership transaction is unavailable",
      );
    }
    existing = await env.APP_DB.prepare(
      "SELECT handle, uid, account_generation, transaction_id, username, tweet_id FROM cf_twitter_ownership_claims WHERE handle = ? LIMIT 1",
    )
      .bind(handle)
      .first<ClaimRow>();
  }
  if (!existing || existing.uid !== uid) {
    return { conflict: true, status: "conflict" } as const;
  }
  if (existing.account_generation !== generation) {
    return { conflict: true, status: "conflict" } as const;
  }
  return {
    conflict: false,
    status: existing.transaction_id === transactionId ? "claimed" : "already_claimed",
    created: inserted && existing.transaction_id === transactionId && existing.uid === uid,
  } as const;
}

async function verifyOwnership(
  c: OwnershipContext,
  context: SignedAuthContext,
  dependencies: TwitterOwnershipDependencies,
  options: { exact: boolean } = { exact: false },
) {
  if (!validUid(context.uid)) {
    throw new TwitterOwnershipError(403, "invalid_principal", "invalid principal");
  }
  const username = normalizeUsername(c.req.query("username"));
  const handle = normalizeHandle(c.req.query("handle"));
  const personaValue = c.req.query("persona_id");
  const personaId = normalizePersonaId(personaValue);
  if (!username || !handle || (personaValue !== undefined && !personaId)) {
    throw new TwitterOwnershipError(
      422,
      "invalid_request",
      "username, handle, or persona_id is invalid",
    );
  }
  const generation = await accountState(c.env, context.uid);
  const requestFingerprint = await sha256Hex(
    `twitter-ownership:${options.exact ? "exact" : "evidence"}:v1\0${stableJson({ handle, personaId, username })}`,
  );
  const providerRequestFingerprint = await sha256Hex(
    `rapidapi-timeline:v1\0${handle}`,
  );
  const transactionId = `tw-own-${requestFingerprint.slice(0, 48)}`;
  let row = await readTransaction(c.env, context.uid, requestFingerprint);
  if (row && row.account_generation !== generation) {
    throw new TwitterOwnershipError(
      409,
      "account_generation_conflict",
      "account generation changed",
    );
  }
  if (
    row &&
    (row.status === "verified" ||
      row.status === "unverified" ||
      row.status === "conflict")
  ) {
    if (row.status === "conflict") {
      throw new TwitterOwnershipError(
        409,
        "twitter_handle_already_claimed",
        "Twitter handle is already claimed",
      );
    }
    return publicResult(row, row.status === "verified" ? "claimed" : null, options.exact);
  }
  const now = nowSeconds(dependencies);
  if (!row) {
    try {
      await c.env.APP_DB.prepare(
        "INSERT INTO cf_twitter_ownership_transactions (uid, transaction_id, account_generation, username, handle, persona_id, provider, provider_request_fingerprint, request_fingerprint, status, verified, result_json, attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'rapidapi-timeline', ?, ?, 'pending', 0, '{}', 0, ?, ?)",
      )
        .bind(
          context.uid,
          transactionId,
          generation,
          username,
          handle,
          personaId,
          providerRequestFingerprint,
          requestFingerprint,
          now,
          now,
        )
        .run();
    } catch {
      row = await readTransaction(c.env, context.uid, requestFingerprint);
      if (!row) {
        throw new TwitterOwnershipError(
          503,
          "transaction_unavailable",
          "Twitter ownership transaction is unavailable",
        );
      }
    }
    row = await readTransaction(c.env, context.uid, requestFingerprint);
  }
  if (!row) {
    throw new TwitterOwnershipError(
      503,
      "transaction_unavailable",
      "Twitter ownership transaction is unavailable",
    );
  }
  await c.env.APP_DB.prepare(
    "UPDATE cf_twitter_ownership_transactions SET attempts = attempts + 1, status = 'pending', last_error = NULL, updated_at = ? WHERE uid = ? AND transaction_id = ? AND account_generation = ? AND status IN ('pending', 'failed')",
  )
    .bind(now, context.uid, transactionId, generation)
    .run();
  let result: OwnershipResult;
  try {
    result = await verifyLatestTwitterTweet(
      c.env,
      username,
      handle,
      dependencies,
    );
  } catch (error) {
    const providerError = providerErrorResponse(error);
    await markFailed(c.env, context.uid, transactionId, now, providerError.code);
    throw providerError;
  }
  let claimStatus: string | null = null;
  if (result.verified && result.tweetId) {
    const claim = await claimHandle(
      c.env,
      context.uid,
      generation,
      transactionId,
      username,
      handle,
      result.tweetId,
      now,
    );
    if (claim.conflict) {
      const conflictJson = JSON.stringify({
        tweet: result.tweet,
        tweet_id: result.tweetId,
        verified: false,
      });
      await c.env.APP_DB.prepare(
        "UPDATE cf_twitter_ownership_transactions SET status = 'conflict', verified = 0, provider_response_fingerprint = ?, result_json = ?, last_error = 'twitter_handle_already_claimed', updated_at = ? WHERE uid = ? AND transaction_id = ? AND account_generation = ?",
      )
        .bind(
          result.providerResponseFingerprint,
          conflictJson,
          now,
          context.uid,
          transactionId,
          generation,
        )
        .run();
      throw new TwitterOwnershipError(
        409,
        "twitter_handle_already_claimed",
        "Twitter handle is already claimed",
      );
    }
    claimStatus = claim.status;
    if (options.exact) {
      try {
        const profile = await fetchTwitterProfile(c.env, handle, {
          fetchImpl: dependencies.fetchImpl,
        });
        result = {
          ...result,
          personaId: await attachTwitterPersona(
            c.env,
            context.uid,
            username,
            handle,
            profile,
            personaId,
            now,
          ),
        };
      } catch (error) {
        // A claim is provisional until the D1 catalog projection commits. Do
        // not strand it when the provider/catalog authority is unavailable.
        if (claim.created) {
          await c.env.APP_DB.prepare(
            "DELETE FROM cf_twitter_ownership_claims WHERE handle = ? AND uid = ? AND transaction_id = ?",
          )
            .bind(handle, context.uid, transactionId)
            .run()
            .catch(() => undefined);
        }
        if (error instanceof TwitterOwnershipError) throw error;
        throw providerErrorResponse(error);
      }
    }
  }
  const status = result.verified ? "verified" : "unverified";
  const resultJson = JSON.stringify({
    tweet: result.tweet,
    tweet_id: result.tweetId,
    verified: result.verified,
    ...(result.personaId ? { persona_id: result.personaId } : {}),
  });
  const updated = await c.env.APP_DB.prepare(
    "UPDATE cf_twitter_ownership_transactions SET status = ?, verified = ?, tweet_id = ?, provider_response_fingerprint = ?, result_json = ?, last_error = NULL, updated_at = ? WHERE uid = ? AND transaction_id = ? AND account_generation = ? AND status = 'pending'",
  )
    .bind(
      status,
      result.verified ? 1 : 0,
      result.tweetId,
      result.providerResponseFingerprint,
      resultJson,
      now,
      context.uid,
      transactionId,
      generation,
    )
    .run();
  if (Number(updated.meta?.changes || 0) !== 1) {
    throw new TwitterOwnershipError(
      409,
      "transaction_replayed",
      "Twitter ownership transaction was already settled",
    );
  }
  row = await readTransaction(c.env, context.uid, requestFingerprint);
  if (!row) {
    throw new TwitterOwnershipError(
      503,
      "transaction_unavailable",
      "Twitter ownership transaction is unavailable",
    );
  }
  return publicResult(row, claimStatus, options.exact);
}

function errorResponse(c: OwnershipContext, error: unknown) {
  if (error instanceof TwitterOwnershipError) {
    return c.json(
      { error: error.code, detail: error.message },
      error.status,
    );
  }
  return c.json(
    { error: "twitter_ownership_unavailable", detail: "Twitter ownership is unavailable" },
    503,
  );
}

/**
 * Register only the namespaced, dormant seam.  The legacy exact route is not
 * registered here and remains protected by the Edge fail-closed boundary.
 */
export function registerTwitterOwnershipRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
  dependencies: TwitterOwnershipDependencies = {},
) {
  app.get("/v2/cf/personas/twitter/verify-ownership", async (c) => {
    if (c.env.TWITTER_OWNERSHIP_STAGING_ENABLED !== "true") {
      return c.json({ error: "twitter_ownership_unavailable" }, 404);
    }
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return c.json(await verifyOwnership(c, context, dependencies));
    } catch (error) {
      return errorResponse(c, error);
    }
  });
  app.get("/v1/personas/twitter/verify-ownership", async (c) => {
    if (c.env.TWITTER_OWNERSHIP_EXACT_STAGING_ENABLED !== "true") {
      return c.json(
        { error: "twitter_ownership_unavailable" },
        503,
        { "cache-control": "no-store" },
      );
    }
    const context = await requestContext(c);
    if (!context) return c.json({ detail: "Not authenticated" }, 401);
    try {
      return c.json(await verifyOwnership(c, context, dependencies, { exact: true }));
    } catch (error) {
      return errorResponse(c, error);
    }
  });
}
