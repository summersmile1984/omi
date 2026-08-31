import type { Context, Hono } from "hono";
import type { JobsEnv } from "./env";

const REVIEW_PATH = "/internal/desktop-release-history/reviews";
const MAX_BODY_BYTES = 256 * 1024;
const REVIEW_TTL_SECONDS = 60 * 60;
const MAX_MANIFEST_BYTES = 128_000;
const SHA256 = /^[0-9a-f]{64}$/;
const SHA256_PREFIXED = /^sha256:[0-9a-f]{64}$/;
const SOURCE_SHA = /^[0-9a-f]{40}$/;
const RELEASE_ID = /^v(?<version>[0-9]+\.[0-9]+(?:\.[0-9]+)?)\+(?<build>[1-9][0-9]*)-macos$/;
const EVIDENCE = /^qualification-evidence-[^/]+\.json$/;
const ENVIRONMENT = /^desktop-backend-env-v[1-9][0-9]*$/;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/;
const MANIFEST_FIELDS = new Set([
  "schema_version",
  "release_id",
  "platform",
  "version",
  "build_number",
  "app_source_sha",
  "zip_url",
  "zip_sha256",
  "dmg_url",
  "dmg_sha256",
  "ed_signature",
  "qualification_evidence_asset",
  "qualification_evidence_sha256",
  "qualification_tier",
  "qualification_passed",
  "backend_mode",
  "desktop_backend_source_sha",
  "desktop_backend_oci_index_digest",
  "desktop_backend_platform_digest",
  "compatibility_contract",
  "environment_contract_version",
  "created_at",
  "published_at",
  "changelog",
  "mandatory",
]);
const REQUIRED_MANIFEST_FIELDS = new Set([
  "schema_version",
  "release_id",
  "platform",
  "version",
  "build_number",
  "app_source_sha",
  "zip_url",
  "zip_sha256",
  "dmg_url",
  "dmg_sha256",
  "ed_signature",
  "qualification_evidence_asset",
  "qualification_evidence_sha256",
  "qualification_tier",
  "qualification_passed",
  "backend_mode",
  "compatibility_contract",
  "environment_contract_version",
  "created_at",
]);
const BACKEND_FIELDS = new Set([
  "desktop_backend_source_sha",
  "desktop_backend_oci_index_digest",
  "desktop_backend_platform_digest",
]);

type JobsContext = Context<{ Bindings: JobsEnv }>;
type JsonObject = Record<string, unknown>;

type ReviewPlan = {
  mode: "dry-run";
  schema_version: 1;
  source: {
    kind: "legacy-api";
    endpoint: string;
    release_id: string;
    manifest_sha256: string;
  };
  manifest: JsonObject;
  manifest_sha256: string;
  plan_hash: string;
  action: "stage";
  status: "planned";
};

type ReviewBatch = {
  review_id: string;
  source_endpoint: string;
  release_id: string;
  manifest_sha256: string;
  plan_hash: string;
  status: "approved" | "applied" | "revoked";
  expires_at: number;
};

type ReviewItem = {
  review_id: string;
  source_endpoint: string;
  release_id: string;
  manifest_sha256: string;
  plan_hash: string;
  manifest_json: string;
};

type ApplyMarker = {
  release_id: string;
  review_id: string;
  manifest_sha256: string;
  plan_hash: string;
  status: "applied";
};

function objectValue(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as JsonObject;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`)
      .join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw new Error("unsupported JSON value");
  return encoded;
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function constantTimeEqual(left: string, right: string): boolean {
  const a = new TextEncoder().encode(left);
  const b = new TextEncoder().encode(right);
  let difference = a.length ^ b.length;
  for (let index = 0; index < Math.max(a.length, b.length); index += 1)
    difference |= (a[index] || 0) ^ (b[index] || 0);
  return difference === 0;
}

function noStoreHeaders(): Record<string, string> {
  return { "cache-control": "no-store" };
}

function adminAuthorized(c: JobsContext): boolean {
  const expected = c.env.ADMIN_KEY;
  const provided = c.req.header("secret-key");
  return Boolean(expected && provided && constantTimeEqual(provided, expected));
}

function gate(c: JobsContext): Response | null {
  if (c.env.DESKTOP_RELEASE_HISTORY_IMPORT_STAGING_ENABLED !== "true")
    return c.json(
      { error: "desktop_release_history_import_unavailable" },
      503,
      noStoreHeaders(),
    );
  if (!adminAuthorized(c))
    return c.json({ error: "unauthorized" }, 403, noStoreHeaders());
  return null;
}

async function readBody(c: JobsContext): Promise<unknown | null> {
  const declared = Number(c.req.header("content-length"));
  if (Number.isFinite(declared) && (declared < 1 || declared > MAX_BODY_BYTES))
    return null;
  const bytes = await c.req.raw.arrayBuffer();
  if (bytes.byteLength < 1 || bytes.byteLength > MAX_BODY_BYTES) return null;
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return null;
  }
}

function validText(value: unknown, maxBytes: number): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    new TextEncoder().encode(value).byteLength <= maxBytes &&
    !/[\u0000-\u001f\u007f]/.test(value)
  );
}

function validUrl(value: unknown, expectedPath: string): value is string {
  if (!validText(value, 2_048)) return false;
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      url.hostname === "github.com" &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      url.pathname === expectedPath
    );
  } catch {
    return false;
  }
}

function validTimestamp(value: unknown): value is string {
  return typeof value === "string" && TIMESTAMP.test(value) && !Number.isNaN(Date.parse(value));
}

function validSha256(value: unknown): value is string {
  return typeof value === "string" && SHA256_PREFIXED.test(value);
}

function validateManifest(value: unknown): JsonObject | null {
  const manifest = objectValue(value);
  if (!manifest || Object.keys(manifest).some((key) => !MANIFEST_FIELDS.has(key))) return null;
  for (const field of REQUIRED_MANIFEST_FIELDS)
    if (!(field in manifest)) return null;
  if (manifest.schema_version !== 1 || manifest.platform !== "macos") return null;
  const releaseId = manifest.release_id;
  const match = typeof releaseId === "string" ? RELEASE_ID.exec(releaseId) : null;
  if (!match || manifest.version !== match.groups?.version || manifest.build_number !== Number(match.groups?.build)) return null;
  if (typeof manifest.app_source_sha !== "string" || !SOURCE_SHA.test(manifest.app_source_sha)) return null;
  if (!validSha256(manifest.zip_sha256) || !validSha256(manifest.dmg_sha256) || !validSha256(manifest.qualification_evidence_sha256)) return null;
  if (!validUrl(manifest.zip_url, `/BasedHardware/omi/releases/download/${releaseId}/Omi.zip`)) return null;
  if (!validUrl(manifest.dmg_url, `/BasedHardware/omi/releases/download/${releaseId}/omi.dmg`)) return null;
  if (!validText(manifest.ed_signature, 4_096)) return null;
  if (!validText(manifest.qualification_evidence_asset, 256) || (!EVIDENCE.test(manifest.qualification_evidence_asset) && !["desktop-smoke-result.json", "desktop-smoke-result-beta.json"].includes(manifest.qualification_evidence_asset))) return null;
  if (!["T2", "signed-smoke", "emergency"].includes(String(manifest.qualification_tier)) || typeof manifest.qualification_passed !== "boolean") return null;
  if (manifest.qualification_tier === "T2" && manifest.qualification_passed !== true) return null;
  if (manifest.qualification_tier !== "T2" && manifest.qualification_passed !== false) return null;
  if (manifest.qualification_tier === "T2" && !EVIDENCE.test(manifest.qualification_evidence_asset)) return null;
  if (manifest.qualification_tier === "signed-smoke" && manifest.qualification_evidence_asset !== "desktop-smoke-result-beta.json") return null;
  if (manifest.qualification_tier === "emergency" && manifest.qualification_evidence_asset !== "desktop-smoke-result.json") return null;
  if (manifest.backend_mode !== "app_only" && manifest.backend_mode !== "backend_required") return null;
  const presentBackendFields = [...BACKEND_FIELDS].filter((field) => field in manifest);
  if (manifest.backend_mode === "app_only" && presentBackendFields.length) return null;
  if (manifest.backend_mode === "backend_required") {
    if (presentBackendFields.length !== BACKEND_FIELDS.size) return null;
    if (manifest.desktop_backend_source_sha !== manifest.app_source_sha || typeof manifest.desktop_backend_source_sha !== "string" || !SOURCE_SHA.test(manifest.desktop_backend_source_sha)) return null;
    if (!validSha256(manifest.desktop_backend_oci_index_digest) || !validSha256(manifest.desktop_backend_platform_digest) || manifest.desktop_backend_oci_index_digest === manifest.desktop_backend_platform_digest) return null;
  }
  if (!validText(manifest.environment_contract_version, 128) || !ENVIRONMENT.test(manifest.environment_contract_version)) return null;
  if (!validTimestamp(manifest.created_at)) return null;
  for (const field of ["published_at"]) if (field in manifest && !validTimestamp(manifest[field])) return null;
  if ("changelog" in manifest && (!Array.isArray(manifest.changelog) || manifest.changelog.length > 100 || manifest.changelog.some((item) => !validText(item, 4_096)))) return null;
  if ("mandatory" in manifest && typeof manifest.mandatory !== "boolean") return null;
  const contract = objectValue(manifest.compatibility_contract);
  if (!contract) return null;
  const contractAllowed = new Set([
    "schema_version",
    "app_release_id",
    "app_version",
    "app_build_number",
    "backend_mode",
    "environment_contract_version",
    ...presentBackendFields,
  ]);
  if (Object.keys(contract).some((key) => !contractAllowed.has(key)) || [...contractAllowed].some((key) => !(key in contract))) return null;
  if (contract.schema_version !== 1 || contract.app_release_id !== releaseId || contract.app_version !== manifest.version || contract.app_build_number !== manifest.build_number || contract.backend_mode !== manifest.backend_mode || contract.environment_contract_version !== manifest.environment_contract_version) return null;
  for (const field of presentBackendFields) if (contract[field] !== manifest[field]) return null;
  const bytes = new TextEncoder().encode(stableJson(manifest));
  return bytes.byteLength <= MAX_MANIFEST_BYTES ? manifest : null;
}

function sourceEndpoint(value: unknown, releaseId: string): string | null {
  if (!validText(value, 2_048)) return null;
  try {
    const url = new URL(value);
    const expectedPath = `/v2/desktop/releases/${encodeURIComponent(releaseId)}`;
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== expectedPath) return null;
    return value;
  } catch {
    return null;
  }
}

async function normalizePlan(value: unknown): Promise<ReviewPlan | null> {
  const plan = objectValue(value);
  const source = objectValue(plan?.source);
  const manifest = validateManifest(plan?.manifest);
  if (!plan || !source || !manifest || plan.mode !== "dry-run" || plan.schema_version !== 1 || plan.action !== "stage" || plan.status !== "planned") return null;
  const allowedPlan = new Set(["mode", "schema_version", "source", "manifest", "manifest_sha256", "plan_hash", "action", "status"]);
  const allowedSource = new Set(["kind", "endpoint", "release_id", "manifest_sha256"]);
  if (Object.keys(plan).some((key) => !allowedPlan.has(key)) || Object.keys(source).some((key) => !allowedSource.has(key))) return null;
  if (source.kind !== "legacy-api" || typeof source.release_id !== "string" || manifest.release_id !== source.release_id) return null;
  const endpoint = sourceEndpoint(source.endpoint, source.release_id);
  if (!endpoint || typeof source.manifest_sha256 !== "string" || !SHA256.test(source.manifest_sha256)) return null;
  const manifestSha = await sha256(stableJson(manifest));
  if (source.manifest_sha256 !== manifestSha || plan.manifest_sha256 !== manifestSha || typeof plan.plan_hash !== "string" || !SHA256.test(plan.plan_hash)) return null;
  const canonicalSource: ReviewPlan["source"] = { kind: "legacy-api", endpoint, release_id: source.release_id, manifest_sha256: manifestSha };
  const expectedPlanHash = await sha256(stableJson({ schema_version: 1, source: canonicalSource, manifest_sha256: manifestSha }));
  if (plan.plan_hash !== expectedPlanHash) return null;
  return {
    mode: "dry-run",
    schema_version: 1,
    source: canonicalSource,
    manifest,
    manifest_sha256: manifestSha,
    plan_hash: expectedPlanHash,
    action: "stage",
    status: "planned",
  };
}

async function review(c: JobsContext): Promise<Response> {
  const plan = await normalizePlan(await readBody(c));
  if (!plan) return c.json({ error: "invalid_desktop_release_history_plan" }, 422, noStoreHeaders());
  const now = Math.floor(Date.now() / 1_000);
  try {
    const existing = await c.env.APP_DB.prepare(
      "SELECT review_id, source_endpoint, release_id, manifest_sha256, plan_hash, status, expires_at FROM cf_desktop_release_import_review_batches WHERE source_endpoint = ? AND release_id = ? AND manifest_sha256 = ? AND plan_hash = ? LIMIT 1",
    ).bind(plan.source.endpoint, plan.source.release_id, plan.manifest_sha256, plan.plan_hash).first<ReviewBatch>();
    if (existing && existing.status !== "revoked" && existing.expires_at > now)
      return c.json({ review_id: existing.review_id, manifest_sha256: existing.manifest_sha256, release_id: existing.release_id, status: existing.status, expires_at: existing.expires_at }, 200, noStoreHeaders());
    const reviewId = crypto.randomUUID();
    const manifestJson = stableJson(plan.manifest);
    await c.env.APP_DB.batch([
      c.env.APP_DB.prepare("INSERT INTO cf_desktop_release_import_review_batches (review_id, source_endpoint, release_id, manifest_sha256, plan_hash, status, reviewed_at, expires_at, updated_at) VALUES (?, ?, ?, ?, ?, 'approved', ?, ?, ?)").bind(reviewId, plan.source.endpoint, plan.source.release_id, plan.manifest_sha256, plan.plan_hash, now, now + REVIEW_TTL_SECONDS, now),
      c.env.APP_DB.prepare("INSERT INTO cf_desktop_release_import_review_items (review_id, source_endpoint, release_id, manifest_sha256, plan_hash, manifest_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)").bind(reviewId, plan.source.endpoint, plan.source.release_id, plan.manifest_sha256, plan.plan_hash, manifestJson, now),
    ]);
    return c.json({ review_id: reviewId, manifest_sha256: plan.manifest_sha256, release_id: plan.source.release_id, status: "approved", expires_at: now + REVIEW_TTL_SECONDS }, 201, noStoreHeaders());
  } catch {
    return c.json({ error: "desktop_release_history_review_unavailable" }, 503, noStoreHeaders());
  }
}

async function apply(c: JobsContext): Promise<Response> {
  const reviewId = c.req.param("reviewId") || "";
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(reviewId)) return c.json({ error: "invalid_request" }, 400, noStoreHeaders());
  const now = Math.floor(Date.now() / 1_000);
  try {
    const batch = await c.env.APP_DB.prepare("SELECT review_id, source_endpoint, release_id, manifest_sha256, plan_hash, status, expires_at FROM cf_desktop_release_import_review_batches WHERE review_id = ? LIMIT 1").bind(reviewId).first<ReviewBatch>();
    if (!batch) return c.json({ error: "not_found" }, 404, noStoreHeaders());
    if (batch.status === "revoked" || batch.expires_at <= now) return c.json({ error: "desktop_release_history_review_expired" }, 409, noStoreHeaders());
    const item = await c.env.APP_DB.prepare("SELECT review_id, source_endpoint, release_id, manifest_sha256, plan_hash, manifest_json FROM cf_desktop_release_import_review_items WHERE review_id = ? LIMIT 1").bind(reviewId).first<ReviewItem>();
    if (!item || item.source_endpoint !== batch.source_endpoint || item.release_id !== batch.release_id || item.manifest_sha256 !== batch.manifest_sha256 || item.plan_hash !== batch.plan_hash) return c.json({ error: "desktop_release_history_review_incomplete" }, 409, noStoreHeaders());
    let storedManifest: JsonObject;
    try {
      storedManifest = JSON.parse(item.manifest_json) as JsonObject;
    } catch {
      return c.json({ error: "desktop_release_history_review_incomplete" }, 409, noStoreHeaders());
    }
    const normalizedStoredPlan = await normalizePlan({
      mode: "dry-run",
      schema_version: 1,
      source: {
        kind: "legacy-api",
        endpoint: item.source_endpoint,
        release_id: item.release_id,
        manifest_sha256: item.manifest_sha256,
      },
      manifest: storedManifest,
      manifest_sha256: item.manifest_sha256,
      plan_hash: item.plan_hash,
      action: "stage",
      status: "planned",
    });
    if (!normalizedStoredPlan || stableJson(normalizedStoredPlan.manifest) !== item.manifest_json) return c.json({ error: "desktop_release_history_review_incomplete" }, 409, noStoreHeaders());
    const existing = await c.env.APP_DB.prepare("SELECT release_id, review_id, manifest_sha256, plan_hash, status FROM cf_desktop_release_import_applies WHERE release_id = ? LIMIT 1").bind(item.release_id).first<ApplyMarker>();
    if (existing) {
      if (existing.review_id !== reviewId || existing.manifest_sha256 !== item.manifest_sha256 || existing.plan_hash !== item.plan_hash || existing.status !== "applied") return c.json({ error: "desktop_release_history_apply_conflict" }, 409, noStoreHeaders());
      return c.json({ status: "applied", release_id: item.release_id, manifest_sha256: item.manifest_sha256, applied_count: 1, already_applied_count: 1 }, 200, noStoreHeaders());
    }
    if (!c.env.API_CORE || !c.env.ADMIN_KEY) return c.json({ error: "desktop_release_history_apply_unavailable" }, 503, noStoreHeaders());
    const response = await c.env.API_CORE.fetch(new Request("https://api-core.internal/v2/desktop/releases", { method: "POST", headers: { "content-type": "application/json", "secret-key": c.env.ADMIN_KEY }, body: item.manifest_json }));
    let body: unknown = null;
    try { body = await response.json(); } catch { /* response body is not trusted */ }
    const responseObject = objectValue(body);
    const responseManifest = objectValue(responseObject?.manifest);
    if (!response.ok || responseObject?.success !== true || responseObject.manifest_sha256 !== undefined && responseObject.manifest_sha256 !== item.manifest_sha256 || responseManifest && stableJson(responseManifest) !== item.manifest_json) return c.json({ error: "desktop_release_history_destination_rejected" }, response.status === 409 ? 409 : 503, noStoreHeaders());
    const appliedAt = Math.floor(Date.now() / 1_000);
    await c.env.APP_DB.batch([
      c.env.APP_DB.prepare("INSERT INTO cf_desktop_release_import_applies (release_id, review_id, manifest_sha256, plan_hash, status, applied_at, updated_at) VALUES (?, ?, ?, ?, 'applied', ?, ?) ON CONFLICT(release_id) DO NOTHING").bind(item.release_id, reviewId, item.manifest_sha256, item.plan_hash, appliedAt, appliedAt),
      c.env.APP_DB.prepare("UPDATE cf_desktop_release_import_review_batches SET status = 'applied', updated_at = ? WHERE review_id = ? AND status = 'approved' AND expires_at > ?").bind(appliedAt, reviewId, now),
    ]);
    const marker = await c.env.APP_DB.prepare("SELECT release_id, review_id, manifest_sha256, plan_hash, status FROM cf_desktop_release_import_applies WHERE release_id = ? LIMIT 1").bind(item.release_id).first<ApplyMarker>();
    if (!marker || marker.review_id !== reviewId || marker.manifest_sha256 !== item.manifest_sha256 || marker.plan_hash !== item.plan_hash || marker.status !== "applied") return c.json({ error: "desktop_release_history_apply_conflict" }, 409, noStoreHeaders());
    return c.json({ status: "applied", release_id: item.release_id, manifest_sha256: item.manifest_sha256, applied_count: 1, already_applied_count: 0 }, 200, noStoreHeaders());
  } catch (error) {
    if (error instanceof Error && error.message.includes("immutable")) return c.json({ error: "desktop_release_history_destination_rejected" }, 409, noStoreHeaders());
    return c.json({ error: "desktop_release_history_apply_unavailable" }, 503, noStoreHeaders());
  }
}

export function registerDesktopReleaseHistoryImportRoutes(app: Hono<{ Bindings: JobsEnv }>): void {
  app.post(REVIEW_PATH, async (c) => {
    const denied = gate(c);
    if (denied) return denied;
    return review(c);
  });
  app.post(`${REVIEW_PATH}/:reviewId/apply`, async (c) => {
    const denied = gate(c);
    if (denied) return denied;
    return apply(c);
  });
}

export const desktopReleaseHistoryImportContract = {
  reviewPath: REVIEW_PATH,
  reviewTtlSeconds: REVIEW_TTL_SECONDS,
  maxBodyBytes: MAX_BODY_BYTES,
};
