import type { Context, Hono } from "hono";
import type { JobsEnv } from "./env";

const REVIEW_PATH = "/internal/phone-history/reviews";
const MAX_BODY_BYTES = 64_000;
const MAX_ENTRIES = 100;
const REVIEW_TTL_SECONDS = 60 * 60;
const MAX_UID_LENGTH = 256;
const MAX_ID_LENGTH = 128;
const MAX_CIPHERTEXT_BYTES = 4_096;
const MAX_FRIENDLY_NAME_BYTES = 256;
const SHA256 = /^[0-9a-f]{64}$/;
const UID = /^[^/\u0000\u0001-\u001f\u007f]{1,256}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SID = /^[A-Za-z]{2}[A-Za-z0-9]{8,160}$/;
const CIPHERTEXT_SEGMENT = /^[A-Za-z0-9_-]+$/;
const E164 = /^\+[1-9]\d{1,14}$/;

type JobsContext = Context<{ Bindings: JobsEnv }>;

type ReviewEntry = {
  uid: string;
  importId: string;
  planHash: string;
};

type LedgerRow = {
  uid: string;
  import_id: string;
  source_record_id: string;
  phone_number_id: string;
  phone_number_hash: string;
  phone_number_ciphertext: string;
  proof_sha256: string;
  source_fingerprint: string;
  source_export_sha256: string;
  twilio_sid: string | null;
  friendly_name: string | null;
  verified_at: number;
  is_primary: number;
  account_generation: number;
  created_at: number;
  updated_at: number;
  plan_hash: string;
  manifest_sha256: string | null;
  action: string;
  status: string;
};

type ReviewBatch = {
  review_id: string;
  manifest_sha256: string;
  entry_count: number;
  status: "approved" | "applied" | "revoked";
  reviewed_at: number;
  expires_at: number;
};

type ExistingPhone = {
  uid: string;
  id: string;
  phone_number_hash: string;
  phone_number_ciphertext: string;
  friendly_name: string | null;
  twilio_sid: string | null;
  verified_at: number;
  is_primary: number;
  account_generation: number;
  created_at: number;
  updated_at: number;
};

type ExistingApply = {
  uid: string;
  import_id: string;
  review_id: string;
  plan_hash: string;
  phone_number_id: string;
  account_generation: number;
  status: string;
};

function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  let difference = leftBytes.length ^ rightBytes.length;
  const maximum = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < maximum; index += 1) {
    difference |= (leftBytes[index] || 0) ^ (rightBytes[index] || 0);
  }
  return difference === 0;
}

function adminAuthorized(c: JobsContext): boolean {
  const expected = c.env.ADMIN_KEY;
  const provided = c.req.header("secret-key");
  return Boolean(expected && provided && constantTimeEqual(provided, expected));
}

function noStoreHeaders(): Record<string, string> {
  return { "cache-control": "no-store" };
}

function gateResponse(c: JobsContext): Response | null {
  if (c.env.PHONE_HISTORY_IMPORT_STAGING_ENABLED !== "true") {
    return c.json({ error: "phone_history_import_unavailable" }, 503, noStoreHeaders());
  }
  if (!adminAuthorized(c)) {
    return c.json({ error: "unauthorized" }, 403, noStoreHeaders());
  }
  return null;
}

async function jsonObject(c: JobsContext): Promise<Record<string, unknown> | null> {
  const declared = Number(c.req.header("content-length"));
  if (Number.isFinite(declared) && (declared < 0 || declared > MAX_BODY_BYTES)) return null;
  const raw = await c.req.text();
  if (!raw || new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function validUid(value: unknown): value is string {
  return typeof value === "string" && value.length <= MAX_UID_LENGTH && UID.test(value);
}

function validId(value: unknown): value is string {
  return typeof value === "string" && value.length <= MAX_ID_LENGTH && IDENTIFIER.test(value);
}

function validHash(value: unknown): value is string {
  return typeof value === "string" && SHA256.test(value);
}

function decodedBase64Url(value: string): Uint8Array | null {
  if (!value || !CIPHERTEXT_SEGMENT.test(value) || value.length % 4 === 1) return null;
  try {
    const padded = `${value}${"=".repeat((4 - (value.length % 4)) % 4)}`;
    const binary = atob(padded.replaceAll("-", "+").replaceAll("_", "/"));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

function validCiphertext(value: unknown): value is string {
  if (typeof value !== "string" || value.length === 0) return false;
  if (new TextEncoder().encode(value).byteLength > MAX_CIPHERTEXT_BYTES) return false;
  const parts = value.split(".");
  if (parts.length !== 2) return false;
  const iv = decodedBase64Url(parts[0]);
  const encrypted = decodedBase64Url(parts[1]);
  return Boolean(
    iv && encrypted && iv.byteLength === 12 && encrypted.byteLength >= 17 &&
      btoa(String.fromCharCode(...iv)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "") === parts[0] &&
      btoa(String.fromCharCode(...encrypted)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "") === parts[1],
  );
}

function hasSensitiveValue(value: unknown): boolean {
  if (typeof value === "string") {
    return E164.test(value.trim().replace(/[\s\-().]+/g, ""));
  }
  if (Array.isArray(value)) return value.some(hasSensitiveValue);
  if (!value || typeof value !== "object") return false;
  const forbidden = new Set([
    "phone",
    "phone_number",
    "e164",
    "plaintext",
    "raw_phone",
    "raw_number",
    "token",
    "secret",
    "authorization",
    "credential",
  ]);
  return Object.entries(value as Record<string, unknown>).some(
    ([key, nested]) => forbidden.has(key.toLowerCase()) || hasSensitiveValue(nested),
  );
}

function parseEntries(value: unknown): ReviewEntry[] | null {
  if (!Array.isArray(value) || value.length < 1 || value.length > MAX_ENTRIES) return null;
  const seen = new Set<string>();
  const entries: ReviewEntry[] = [];
  for (const raw of value) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw) || hasSensitiveValue(raw)) return null;
    const object = raw as Record<string, unknown>;
    if (
      Object.keys(object).some((key) => !["uid", "import_id", "plan_hash"].includes(key)) ||
      !validUid(object.uid) ||
      !validHash(object.import_id) ||
      !validHash(object.plan_hash)
    ) return null;
    const entry = { uid: object.uid, importId: object.import_id, planHash: object.plan_hash };
    const key = `${entry.uid}\0${entry.importId}`;
    if (seen.has(key)) return null;
    seen.add(key);
    entries.push(entry);
  }
  return entries;
}

function validManifest(value: unknown): value is string {
  return validHash(value);
}

function validReviewId(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
}

function validFriendlyName(value: unknown): value is string | null {
  return value === null || (
    typeof value === "string" &&
    value.length > 0 &&
    new TextEncoder().encode(value).byteLength <= MAX_FRIENDLY_NAME_BYTES &&
    !/[\u0000-\u001f\u007f]/.test(value) &&
    !hasSensitiveValue(value)
  );
}

function validLedgerRow(row: LedgerRow, manifestSha256: string, entry: ReviewEntry): boolean {
  return (
    row.uid === entry.uid &&
    row.import_id === entry.importId &&
    row.plan_hash === entry.planHash &&
    row.manifest_sha256 === manifestSha256 &&
    row.action === "stage" &&
    row.status === "planned" &&
    validId(row.phone_number_id) &&
    validHash(row.phone_number_hash) &&
    validCiphertext(row.phone_number_ciphertext) &&
    validHash(row.proof_sha256) &&
    validHash(row.source_fingerprint) &&
    validHash(row.source_export_sha256) &&
    (row.twilio_sid === null || SID.test(row.twilio_sid)) &&
    validFriendlyName(row.friendly_name) &&
    Number.isSafeInteger(Number(row.verified_at)) && Number(row.verified_at) > 0 &&
    (Number(row.is_primary) === 0 || Number(row.is_primary) === 1) &&
    Number.isSafeInteger(Number(row.account_generation)) && Number(row.account_generation) >= 0 &&
    Number.isSafeInteger(Number(row.created_at)) && Number(row.created_at) > 0 &&
    Number.isSafeInteger(Number(row.updated_at)) && Number(row.updated_at) > 0
  );
}

async function readLedgerRows(
  env: JobsEnv,
  entries: ReviewEntry[],
): Promise<LedgerRow[]> {
  const statements = entries.map((entry) =>
    env.APP_DB.prepare(
      "SELECT uid, import_id, source_record_id, phone_number_id, phone_number_hash, phone_number_ciphertext, " +
        "proof_sha256, source_fingerprint, source_export_sha256, twilio_sid, friendly_name, verified_at, " +
        "is_primary, account_generation, created_at, updated_at, plan_hash, manifest_sha256, action, status " +
        "FROM cf_phone_number_import_ledger WHERE uid = ? AND import_id = ? LIMIT 1",
    ).bind(entry.uid, entry.importId),
  );
  const result = await env.APP_DB.batch<LedgerRow>(statements);
  if (result.length !== entries.length || result.some((item) => !item.success || !item.results?.length)) {
    throw new Error("phone import ledger is incomplete");
  }
  return result.map((item) => item.results[0] as LedgerRow);
}

async function review(c: JobsContext): Promise<Response> {
  const body = await jsonObject(c);
  if (!body || hasSensitiveValue(body) || !validManifest(body.manifest_sha256)) {
    return c.json({ error: "invalid_request" }, 422, noStoreHeaders());
  }
  const entries = parseEntries(body.entries);
  if (!entries) return c.json({ error: "invalid_request" }, 422, noStoreHeaders());
  try {
    const ledgerRows = await readLedgerRows(c.env, entries);
    if (ledgerRows.some((row, index) => !validLedgerRow(row, body.manifest_sha256 as string, entries[index]))) {
      return c.json({ error: "phone_import_ledger_not_reviewable" }, 409, noStoreHeaders());
    }
    const now = Math.floor(Date.now() / 1_000);
    const reviewId = crypto.randomUUID();
    const statements = [
      c.env.APP_DB.prepare(
        "INSERT INTO cf_phone_number_import_review_batches " +
          "(review_id, manifest_sha256, entry_count, status, reviewed_at, expires_at, updated_at) " +
          "VALUES (?, ?, ?, 'approved', ?, ?, ?)",
      ).bind(reviewId, body.manifest_sha256, entries.length, now, now + REVIEW_TTL_SECONDS, now),
      ...entries.map((entry) =>
        c.env.APP_DB.prepare(
          "INSERT INTO cf_phone_number_import_review_items (review_id, uid, import_id, plan_hash) VALUES (?, ?, ?, ?)",
        ).bind(reviewId, entry.uid, entry.importId, entry.planHash),
      ),
    ];
    await c.env.APP_DB.batch(statements);
    return c.json(
      {
        review_id: reviewId,
        manifest_sha256: body.manifest_sha256,
        entry_count: entries.length,
        status: "approved",
        expires_at: now + REVIEW_TTL_SECONDS,
      },
      201,
      noStoreHeaders(),
    );
  } catch {
    return c.json({ error: "phone_import_review_unavailable" }, 503, noStoreHeaders());
  }
}

function phoneValuesMatch(row: ExistingPhone, ledger: LedgerRow): boolean {
  return (
    row.uid === ledger.uid &&
    row.id === ledger.phone_number_id &&
    row.phone_number_hash === ledger.phone_number_hash &&
    row.phone_number_ciphertext === ledger.phone_number_ciphertext &&
    row.friendly_name === ledger.friendly_name &&
    row.twilio_sid === ledger.twilio_sid &&
    Number(row.verified_at) === Number(ledger.verified_at) &&
    Number(row.is_primary) === Number(ledger.is_primary) &&
    Number(row.account_generation) === Number(ledger.account_generation) &&
    Number(row.created_at) === Number(ledger.created_at) &&
    Number(row.updated_at) === Number(ledger.updated_at)
  );
}

async function apply(c: JobsContext): Promise<Response> {
  const reviewId = c.req.param("reviewId") || "";
  if (!validReviewId(reviewId)) return c.json({ error: "invalid_request" }, 400, noStoreHeaders());
  const now = Math.floor(Date.now() / 1_000);
  try {
    const review = await c.env.APP_DB.prepare(
      "SELECT review_id, manifest_sha256, entry_count, status, reviewed_at, expires_at FROM " +
        "cf_phone_number_import_review_batches WHERE review_id = ? LIMIT 1",
    ).bind(reviewId).first<ReviewBatch>();
    if (!review) return c.json({ error: "not_found" }, 404, noStoreHeaders());
    if (review.status === "revoked" || review.expires_at <= now) {
      return c.json({ error: "phone_import_review_expired" }, 409, noStoreHeaders());
    }
    const items = await c.env.APP_DB.prepare(
      "SELECT i.uid, i.import_id, i.plan_hash, l.source_record_id, l.phone_number_id, l.phone_number_hash, " +
        "l.phone_number_ciphertext, l.proof_sha256, l.source_fingerprint, l.source_export_sha256, " +
        "l.twilio_sid, l.friendly_name, l.verified_at, l.is_primary, l.account_generation, l.created_at, l.updated_at, l.plan_hash, " +
        "l.manifest_sha256, l.action, l.status " +
        "FROM cf_phone_number_import_review_items AS i LEFT JOIN cf_phone_number_import_ledger AS l " +
        "ON l.uid = i.uid AND l.import_id = i.import_id WHERE i.review_id = ? ORDER BY i.uid, i.import_id",
    ).bind(reviewId).all<LedgerRow & { plan_hash: string }>();
    if (items.results.length !== review.entry_count) {
      return c.json({ error: "phone_import_review_incomplete" }, 409, noStoreHeaders());
    }
    const entries = items.results.map((row) => ({ uid: row.uid, importId: row.import_id, planHash: row.plan_hash }));
    if (items.results.some((row) => !validLedgerRow(row, review.manifest_sha256, { uid: row.uid, importId: row.import_id, planHash: row.plan_hash }))) {
      return c.json({ error: "phone_import_ledger_changed" }, 409, noStoreHeaders());
    }

    const existingApplies = await Promise.all(
      entries.map((entry) =>
        c.env.APP_DB.prepare(
          "SELECT uid, import_id, review_id, plan_hash, phone_number_id, account_generation, status " +
            "FROM cf_phone_number_import_applies WHERE uid = ? AND import_id = ? LIMIT 1",
        ).bind(entry.uid, entry.importId).first<ExistingApply>(),
      ),
    );
    const alreadyApplied = new Set<number>();
    existingApplies.forEach((marker, index) => {
      if (!marker) return;
      const row = items.results[index];
      if (
        marker.review_id !== reviewId ||
        marker.plan_hash !== row.plan_hash ||
        marker.phone_number_id !== row.phone_number_id ||
        Number(marker.account_generation) !== Number(row.account_generation) ||
        marker.status !== "applied"
      ) throw new Error("phone import apply marker conflict");
      alreadyApplied.add(index);
    });

    const activeRows = items.results.filter((_row, index) => !alreadyApplied.has(index));
    const accountChecks = await Promise.all(
      activeRows.map((row) =>
        c.env.APP_DB.prepare(
          "SELECT state, checkpoint_phase, destination_backend_bound, account_generation FROM cf_account_cutover " +
            "WHERE uid = ? LIMIT 1",
        ).bind(row.uid).first<{ state?: unknown; checkpoint_phase?: unknown; destination_backend_bound?: unknown; account_generation?: unknown }>(),
      ),
    );
    const fenceChecks = await Promise.all(
      activeRows.map((row) =>
        c.env.APP_DB.prepare(
          "SELECT 1 AS fenced FROM cf_account_deletion_intents WHERE uid = ? " +
            "UNION ALL SELECT 1 AS fenced FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ? LIMIT 1",
        ).bind(row.uid, row.uid, now).first<{ fenced?: unknown }>(),
      ),
    );
    if (activeRows.some((row, index) => {
      const account = accountChecks[index];
      return (
        fenceChecks[index] ||
        !account ||
        account.state !== "new" ||
        account.checkpoint_phase !== "completed" ||
        Number(account.destination_backend_bound) !== 1 ||
        Number(account.account_generation) !== Number(row.account_generation)
      );
    })) {
      return c.json({ error: "phone_import_authority_changed" }, 409, noStoreHeaders());
    }

    const existingPhones = await Promise.all(
      activeRows.map((row) =>
        c.env.APP_DB.prepare(
          "SELECT uid, id, phone_number_hash, phone_number_ciphertext, friendly_name, twilio_sid, verified_at, " +
            "is_primary, account_generation, created_at, updated_at FROM cf_phone_numbers WHERE uid = ? AND id = ? LIMIT 1",
        ).bind(row.uid, row.phone_number_id).first<ExistingPhone>(),
      ),
    );
    if (activeRows.some((row, index) => existingPhones[index] && !phoneValuesMatch(existingPhones[index] as ExistingPhone, row))) {
      return c.json({ error: "phone_import_conflict" }, 409, noStoreHeaders());
    }
    const globalHashes = await Promise.all(
      activeRows.map((row) =>
        c.env.APP_DB.prepare(
          "SELECT uid, id FROM cf_phone_numbers WHERE phone_number_hash = ? LIMIT 1",
        ).bind(row.phone_number_hash).first<{ uid?: unknown; id?: unknown }>(),
      ),
    );
    const globalSids = await Promise.all(
      activeRows.map((row) => row.twilio_sid
        ? c.env.APP_DB.prepare("SELECT uid, id FROM cf_phone_numbers WHERE twilio_sid = ? LIMIT 1").bind(row.twilio_sid).first<{ uid?: unknown; id?: unknown }>()
        : Promise.resolve(null)),
    );
    if (activeRows.some((row, index) => {
      const hash = globalHashes[index];
      const sid = globalSids[index];
      return (
        (hash && (hash.uid !== row.uid || hash.id !== row.phone_number_id)) ||
        (sid && (sid.uid !== row.uid || sid.id !== row.phone_number_id))
      );
    })) {
      return c.json({ error: "phone_import_conflict" }, 409, noStoreHeaders());
    }

    const statements = activeRows.flatMap((row) => [
      c.env.APP_DB.prepare(
        "INSERT INTO cf_phone_numbers " +
          "(uid, id, phone_number_hash, phone_number_ciphertext, friendly_name, twilio_sid, verified_at, is_primary, " +
          "account_generation, created_at, updated_at) " +
          "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? " +
          "WHERE EXISTS (SELECT 1 FROM cf_account_cutover WHERE uid = ? AND state = 'new' AND checkpoint_phase = 'completed' " +
          "AND destination_backend_bound = 1 AND account_generation = ?) " +
          "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) " +
          "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?) " +
          "ON CONFLICT(uid, id) DO NOTHING",
      ).bind(
        row.uid, row.phone_number_id, row.phone_number_hash, row.phone_number_ciphertext,
        row.friendly_name, row.twilio_sid, row.verified_at, row.is_primary, row.account_generation,
        row.created_at, row.updated_at, row.uid, row.account_generation, row.uid, row.uid, now,
      ),
      c.env.APP_DB.prepare(
        "INSERT INTO cf_phone_number_import_applies " +
          "(uid, import_id, review_id, plan_hash, phone_number_id, account_generation, status, applied_at, updated_at) " +
          "VALUES (?, ?, ?, ?, ?, ?, 'applied', ?, ?) ON CONFLICT(uid, import_id) DO NOTHING",
      ).bind(row.uid, row.import_id, reviewId, row.plan_hash, row.phone_number_id, row.account_generation, now, now),
    ]);
    if (statements.length > 0) {
      const results = await c.env.APP_DB.batch(statements);
      for (let index = 0; index < activeRows.length; index += 1) {
        const markerResult = results[index * 2 + 1];
        if (!markerResult?.success || Number(markerResult.meta?.changes || 0) !== 1) {
          throw new Error("phone import apply marker was not committed");
        }
      }
    }
    await c.env.APP_DB.prepare(
      "UPDATE cf_phone_number_import_review_batches SET status = 'applied', updated_at = ? WHERE review_id = ? AND status = 'approved' AND expires_at > ?",
    ).bind(now, reviewId, now).run();
    return c.json(
      {
        review_id: reviewId,
        status: "applied",
        entry_count: entries.length,
        applied_count: activeRows.length,
        already_applied_count: alreadyApplied.size,
      },
      200,
      noStoreHeaders(),
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (message.includes("account deletion fence")) {
      return c.json({ error: "account_deletion_fence" }, 409, noStoreHeaders());
    }
    if (message.includes("conflict") || message.includes("authority")) {
      return c.json({ error: "phone_import_authority_changed" }, 409, noStoreHeaders());
    }
    return c.json({ error: "phone_import_apply_unavailable" }, 503, noStoreHeaders());
  }
}

export function registerPhoneHistoryImportRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
): void {
  app.post(REVIEW_PATH, async (c) => {
    const gated = gateResponse(c);
    if (gated) return gated;
    return review(c);
  });
  app.post(`${REVIEW_PATH}/:reviewId/apply`, async (c) => {
    const gated = gateResponse(c);
    if (gated) return gated;
    return apply(c);
  });
}

export const phoneHistoryImportConstants = Object.freeze({
  reviewPath: REVIEW_PATH,
  maxEntries: MAX_ENTRIES,
  reviewTtlSeconds: REVIEW_TTL_SECONDS,
});
