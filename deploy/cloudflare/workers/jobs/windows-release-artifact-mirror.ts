import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import type { Context, Hono } from "hono";
import type { Message } from "@cloudflare/workers-types";
import type { JobMessage, JobsEnv } from "./env";

/**
 * Reviewed Windows release artifact transfer boundary.
 *
 * The operator supplies the deterministic plan emitted by
 * .github/scripts/windows_release_history.py.  This worker does not read
 * GitHub history or mutate the desktop release/channel authority; it only
 * copies the three reviewed bytes into a content-bound R2 ledger.
 */

export const WINDOWS_ARTIFACT_MIRROR_KIND =
  "windows_release_artifact_mirror" as const;
export const WINDOWS_ARTIFACT_SYSTEM_UID = "system:windows-release-artifact";
export const WINDOWS_ARTIFACT_MAX_BYTES = 1_073_741_824;
export const WINDOWS_ARTIFACT_MAX_REDIRECTS = 3;
export const WINDOWS_ARTIFACT_MAX_ATTEMPTS = 3;
export const WINDOWS_ARTIFACT_LEASE_SECONDS = 15 * 60;
export const WINDOWS_ARTIFACT_RETRY_SECONDS = 30;

const REVIEW_PATH = "/internal/windows-release-history/reviews";
const MAX_BODY_BYTES = 256 * 1024;
const REVIEW_TTL_SECONDS = 60 * 60;
const RELEASE_ID = /^v[0-9]+\.[0-9]+\.[0-9]+-windows$/;
const SHA256 = /^[0-9a-f]{64}$/;
const ASSET_KEYS = ["exe", "blockmap", "latest_yml"] as const;
const SOURCE_HOSTS = new Set([
  "github.com",
  "release-assets.githubusercontent.com",
  "objects.githubusercontent.com",
  "githubusercontent.com",
]);
const ASSET_NAMES: Record<(typeof ASSET_KEYS)[number], string> = {
  exe: "Omi-for-Windows-Setup-{version}.exe",
  blockmap: "Omi-for-Windows-Setup-{version}.exe.blockmap",
  latest_yml: "latest.yml",
};
const CONTENT_TYPES: Record<(typeof ASSET_KEYS)[number], string> = {
  exe: "application/vnd.microsoft.portable-executable",
  blockmap: "application/json",
  latest_yml: "text/yaml",
};

type JsonObject = Record<string, unknown>;
type AssetKey = (typeof ASSET_KEYS)[number];

export type WindowsArtifactSpec = {
  releaseId: string;
  assetKey: AssetKey;
  assetName: string;
  sourceUrl: string;
  objectKey: string;
  expectedSha256: string;
  contentType: string;
};

type WindowsReleasePlan = {
  mode: "dry-run";
  schema_version: 1;
  source: {
    kind: "github-release";
    repository: "BasedHardware/omi";
    release_id: string;
    release_fingerprint: string;
  };
  release: {
    release_id: string;
    version: string;
    build_number: number;
    prerelease: boolean;
    channel: "beta" | "stable";
    assets: Record<AssetKey, { url: string; sha256: string }>;
  };
  action: "stage";
  status: "planned";
  plan_hash: string;
};

type ReviewBatch = {
  review_id: string;
  release_id: string;
  source_fingerprint: string;
  plan_hash: string;
  status: "approved" | "applied" | "revoked";
  expires_at: number;
};

type ReviewItem = {
  review_id: string;
  release_id: string;
  source_fingerprint: string;
  plan_hash: string;
  plan_json: string;
};

type ArtifactRow = {
  release_id: string;
  asset_name: string;
  review_id: string;
  source_fingerprint: string;
  plan_hash: string;
  source_url: string;
  object_key: string;
  expected_sha256: string;
  content_type: string;
  status: "queued" | "copying" | "copied" | "failed";
  attempts: number;
  last_queued_at: number | null;
};

type ArtifactPayload = {
  releaseId: string;
  assetKey: AssetKey;
  assetName: string;
  sourceUrl: string;
  objectKey: string;
  expectedSha256: string;
  contentType: string;
  reviewId: string;
  sourceFingerprint: string;
  planHash: string;
};

type WindowsMirrorMessage = Message<JobMessage> & {
  body: JobMessage & { kind: typeof WINDOWS_ARTIFACT_MIRROR_KIND };
};

export class WindowsArtifactMirrorError extends Error {
  readonly retryable: boolean;
  readonly code: string;

  constructor(code: string, retryable: boolean) {
    super(code);
    this.name = "WindowsArtifactMirrorError";
    this.code = code;
    this.retryable = retryable;
  }
}

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

function digest(value: string): string {
  return bytesToHex(sha256(new TextEncoder().encode(value)));
}

function validText(value: unknown, maxBytes: number): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    new TextEncoder().encode(value).byteLength <= maxBytes &&
    !/[\u0000-\u001f\u007f]/.test(value)
  );
}

function validUuid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      value,
    )
  );
}

function noStoreHeaders(): Record<string, string> {
  return { "cache-control": "no-store" };
}

function canonicalUrl(releaseId: string, assetName: string): string {
  return `https://github.com/BasedHardware/omi/releases/download/${releaseId}/${assetName}`;
}

function objectKey(releaseId: string, assetName: string): string {
  return `desktop-windows-releases/${releaseId}/${assetName}`;
}

function canonicalAssetName(assetKey: AssetKey, version: string): string {
  return ASSET_NAMES[assetKey].replace("{version}", version);
}

function validCleanGithubUrl(value: unknown, expected: string): value is string {
  if (!validText(value, 2_048)) return false;
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      url.hostname === "github.com" &&
      url.host === "github.com" &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      !url.pathname.includes("%") &&
      !url.pathname.includes("/../") &&
      !url.pathname.includes("/./") &&
      url.pathname === new URL(expected).pathname
    );
  } catch {
    return false;
  }
}

export function normalizedWindowsReleasePlan(value: unknown): WindowsReleasePlan | null {
  const plan = objectValue(value);
  const source = objectValue(plan?.source);
  const release = objectValue(plan?.release);
  if (!plan || !source || !release) return null;
  const allowedPlan = new Set([
    "mode",
    "schema_version",
    "source",
    "release",
    "action",
    "status",
    "plan_hash",
  ]);
  const allowedSource = new Set([
    "kind",
    "repository",
    "release_id",
    "release_fingerprint",
  ]);
  const allowedRelease = new Set([
    "release_id",
    "version",
    "build_number",
    "prerelease",
    "channel",
    "assets",
  ]);
  if (
    Object.keys(plan).some((key) => !allowedPlan.has(key)) ||
    Object.keys(source).some((key) => !allowedSource.has(key)) ||
    Object.keys(release).some((key) => !allowedRelease.has(key)) ||
    plan.mode !== "dry-run" ||
    plan.schema_version !== 1 ||
    plan.action !== "stage" ||
    plan.status !== "planned"
  ) return null;
  const releaseId = release.release_id;
  const version = release.version;
  const buildNumber = release.build_number;
  if (
    typeof releaseId !== "string" ||
    !RELEASE_ID.test(releaseId) ||
    typeof version !== "string" ||
    releaseId !== `v${version}-windows` ||
    !/^[0-9]+\.[0-9]+\.[0-9]+$/.test(version) ||
    typeof buildNumber !== "number" ||
    !Number.isSafeInteger(buildNumber) ||
    buildNumber <= 0 ||
    typeof release.prerelease !== "boolean" ||
    (release.prerelease && release.channel !== "beta") ||
    (!release.prerelease && release.channel !== "stable")
  ) return null;
  if (
    source.kind !== "github-release" ||
    source.repository !== "BasedHardware/omi" ||
    source.release_id !== releaseId ||
    typeof source.release_fingerprint !== "string" ||
    !SHA256.test(source.release_fingerprint)
  ) return null;
  const assets = objectValue(release.assets);
  if (!assets || Object.keys(assets).some((key) => !ASSET_KEYS.includes(key as AssetKey))) return null;
  const normalizedAssets = {} as Record<AssetKey, { url: string; sha256: string }>;
  for (const assetKey of ASSET_KEYS) {
    const metadata = objectValue(assets[assetKey]);
    const assetName = canonicalAssetName(assetKey, version);
    const expectedUrl = canonicalUrl(releaseId, assetName);
    if (
      !metadata ||
      Object.keys(metadata).some((key) => !["url", "sha256"].includes(key)) ||
      !validCleanGithubUrl(metadata.url, expectedUrl) ||
      typeof metadata.sha256 !== "string" ||
      !SHA256.test(metadata.sha256)
    ) return null;
    normalizedAssets[assetKey] = { url: metadata.url, sha256: metadata.sha256 };
  }
  if (
    typeof plan.plan_hash !== "string" ||
    !SHA256.test(plan.plan_hash)
  ) return null;
  const withoutHash = {
    mode: "dry-run",
    schema_version: 1,
    source: {
      kind: "github-release",
      repository: "BasedHardware/omi",
      release_id: releaseId,
      release_fingerprint: source.release_fingerprint,
    },
    release: {
      release_id: releaseId,
      version,
      build_number: buildNumber,
      prerelease: release.prerelease,
      channel: release.channel,
      assets: normalizedAssets,
    },
    action: "stage",
    status: "planned",
  };
  if (digest(stableJson(withoutHash)) !== plan.plan_hash) return null;
  return { ...withoutHash, plan_hash: plan.plan_hash } as WindowsReleasePlan;
}

function specsForPlan(plan: WindowsReleasePlan): WindowsArtifactSpec[] {
  return ASSET_KEYS.map((assetKey) => {
    const assetName = canonicalAssetName(assetKey, plan.release.version);
    const metadata = plan.release.assets[assetKey];
    return {
      releaseId: plan.release.release_id,
      assetKey,
      assetName,
      sourceUrl: metadata.url,
      objectKey: objectKey(plan.release.release_id, assetName),
      expectedSha256: metadata.sha256,
      contentType: CONTENT_TYPES[assetKey],
    };
  });
}

function artifactPayload(
  spec: WindowsArtifactSpec,
  reviewId: string,
  sourceFingerprint: string,
  planHash: string,
): ArtifactPayload {
  return {
    releaseId: spec.releaseId,
    assetKey: spec.assetKey,
    assetName: spec.assetName,
    sourceUrl: spec.sourceUrl,
    objectKey: spec.objectKey,
    expectedSha256: spec.expectedSha256,
    contentType: spec.contentType,
    reviewId,
    sourceFingerprint,
    planHash,
  };
}

export function windowsArtifactJobId(payload: ArtifactPayload): string {
  return `windows-artifact-${digest(
    `${payload.releaseId}\0${payload.assetName}\0${payload.expectedSha256}\0${payload.planHash}`,
  ).slice(0, 48)}`;
}

async function readBody(c: Context<{ Bindings: JobsEnv }>): Promise<unknown | null> {
  const declared = Number(c.req.header("content-length"));
  if (Number.isFinite(declared) && (declared < 1 || declared > MAX_BODY_BYTES)) return null;
  const bytes = await c.req.raw.arrayBuffer();
  if (!bytes.byteLength || bytes.byteLength > MAX_BODY_BYTES) return null;
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return null;
  }
}

function adminAuthorized(c: Context<{ Bindings: JobsEnv }>): boolean {
  const expected = c.env.ADMIN_KEY;
  const provided = c.req.header("secret-key");
  if (!expected || !provided || expected.length !== provided.length) return false;
  let difference = 0;
  for (let index = 0; index < expected.length; index += 1)
    difference |= expected.charCodeAt(index) ^ provided.charCodeAt(index);
  return difference === 0;
}

function gate(c: Context<{ Bindings: JobsEnv }>): Response | null {
  if (c.env.WINDOWS_RELEASE_HISTORY_IMPORT_STAGING_ENABLED !== "true")
    return c.json({ error: "windows_release_history_import_unavailable" }, 503, noStoreHeaders());
  if (!adminAuthorized(c)) return c.json({ error: "unauthorized" }, 403, noStoreHeaders());
  return null;
}

async function review(c: Context<{ Bindings: JobsEnv }>): Promise<Response> {
  const plan = normalizedWindowsReleasePlan(await readBody(c));
  if (!plan) return c.json({ error: "invalid_windows_release_history_plan" }, 422, noStoreHeaders());
  const now = Math.floor(Date.now() / 1_000);
  try {
    const existing = await c.env.APP_DB.prepare(
      "SELECT review_id, release_id, source_fingerprint, plan_hash, status, expires_at FROM cf_windows_release_artifact_review_batches WHERE release_id = ? AND source_fingerprint = ? AND plan_hash = ? LIMIT 1",
    ).bind(plan.release.release_id, plan.source.release_fingerprint, plan.plan_hash).first<ReviewBatch>();
    if (existing && existing.status !== "revoked" && existing.expires_at > now)
      return c.json(existing, 200, noStoreHeaders());
    const reviewId = crypto.randomUUID();
    const planJson = stableJson(plan);
    await c.env.APP_DB.batch([
      c.env.APP_DB.prepare(
        "INSERT INTO cf_windows_release_artifact_review_batches (review_id, release_id, source_fingerprint, plan_hash, status, reviewed_at, expires_at, updated_at) VALUES (?, ?, ?, ?, 'approved', ?, ?, ?)",
      ).bind(reviewId, plan.release.release_id, plan.source.release_fingerprint, plan.plan_hash, now, now + REVIEW_TTL_SECONDS, now),
      c.env.APP_DB.prepare(
        "INSERT INTO cf_windows_release_artifact_review_items (review_id, release_id, source_fingerprint, plan_hash, plan_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
      ).bind(reviewId, plan.release.release_id, plan.source.release_fingerprint, plan.plan_hash, planJson, now),
    ]);
    return c.json({ review_id: reviewId, release_id: plan.release.release_id, plan_hash: plan.plan_hash, status: "approved", expires_at: now + REVIEW_TTL_SECONDS }, 201, noStoreHeaders());
  } catch {
    return c.json({ error: "windows_release_history_review_unavailable" }, 503, noStoreHeaders());
  }
}

async function readReview(c: Context<{ Bindings: JobsEnv }>, reviewId: string): Promise<{ batch: ReviewBatch; item: ReviewItem; plan: WindowsReleasePlan } | Response> {
  if (!validUuid(reviewId)) return c.json({ error: "invalid_request" }, 400, noStoreHeaders());
  const batch = await c.env.APP_DB.prepare(
    "SELECT review_id, release_id, source_fingerprint, plan_hash, status, expires_at FROM cf_windows_release_artifact_review_batches WHERE review_id = ? LIMIT 1",
  ).bind(reviewId).first<ReviewBatch>();
  if (!batch) return c.json({ error: "not_found" }, 404, noStoreHeaders());
  const item = await c.env.APP_DB.prepare(
    "SELECT review_id, release_id, source_fingerprint, plan_hash, plan_json FROM cf_windows_release_artifact_review_items WHERE review_id = ? LIMIT 1",
  ).bind(reviewId).first<ReviewItem>();
  if (!item || item.release_id !== batch.release_id || item.source_fingerprint !== batch.source_fingerprint || item.plan_hash !== batch.plan_hash)
    return c.json({ error: "windows_release_history_review_incomplete" }, 409, noStoreHeaders());
  let parsed: unknown;
  try { parsed = JSON.parse(item.plan_json); } catch { parsed = null; }
  const plan = normalizedWindowsReleasePlan(parsed);
  if (!plan || stableJson(plan) !== item.plan_json) return c.json({ error: "windows_release_history_review_incomplete" }, 409, noStoreHeaders());
  return { batch, item, plan };
}

async function apply(c: Context<{ Bindings: JobsEnv }>): Promise<Response> {
  const result = await readReview(c, c.req.param("reviewId") || "");
  if (result instanceof Response) return result;
  const { batch, item } = result;
  const now = Math.floor(Date.now() / 1_000);
  if (batch.status === "revoked" || batch.expires_at <= now) return c.json({ error: "windows_release_history_review_expired" }, 409, noStoreHeaders());
  if (batch.status === "applied") return c.json({ status: "applied", release_id: item.release_id, plan_hash: item.plan_hash, already_applied: true }, 200, noStoreHeaders());
  try {
    const changed = await c.env.APP_DB.prepare(
      "UPDATE cf_windows_release_artifact_review_batches SET status = 'applied', updated_at = ? WHERE review_id = ? AND status = 'approved' AND expires_at > ?",
    ).bind(now, batch.review_id, now).run();
    if (Number(changed?.meta?.changes || 0) !== 1) return c.json({ error: "windows_release_history_apply_conflict" }, 409, noStoreHeaders());
    return c.json({ status: "applied", release_id: item.release_id, plan_hash: item.plan_hash, already_applied: false }, 200, noStoreHeaders());
  } catch {
    return c.json({ error: "windows_release_history_apply_unavailable" }, 503, noStoreHeaders());
  }
}

async function applyArtifacts(c: Context<{ Bindings: JobsEnv }>): Promise<Response> {
  const result = await readReview(c, c.req.param("reviewId") || "");
  if (result instanceof Response) return result;
  const { batch, item, plan } = result;
  if (batch.status !== "applied") return c.json({ error: "windows_release_history_apply_required" }, 409, noStoreHeaders());
  const specs = specsForPlan(plan);
  const now = Math.floor(Date.now() / 1_000);
  try {
    await c.env.APP_DB.batch(specs.map((spec) => c.env.APP_DB.prepare(
      "INSERT INTO cf_windows_release_artifacts (release_id, asset_name, review_id, source_fingerprint, plan_hash, source_url, object_key, expected_sha256, content_type, status, attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?) ON CONFLICT(release_id, asset_name) DO NOTHING",
    ).bind(spec.releaseId, spec.assetName, item.review_id, item.source_fingerprint, item.plan_hash, spec.sourceUrl, spec.objectKey, spec.expectedSha256, spec.contentType, now, now)));
    const rows = await c.env.APP_DB.prepare(
      "SELECT release_id, asset_name, review_id, source_fingerprint, plan_hash, source_url, object_key, expected_sha256, content_type, status, attempts, last_queued_at FROM cf_windows_release_artifacts WHERE release_id = ? ORDER BY asset_name",
    ).bind(plan.release.release_id).all<ArtifactRow>();
    const ledger = rows.results || [];
    if (ledger.length !== specs.length || ledger.some((row) => {
      const spec = specs.find((entry) => entry.assetName === row.asset_name);
      return !spec || row.review_id !== item.review_id || row.source_fingerprint !== item.source_fingerprint || row.plan_hash !== item.plan_hash || row.source_url !== spec.sourceUrl || row.object_key !== spec.objectKey || row.expected_sha256 !== spec.expectedSha256 || row.content_type !== spec.contentType;
    })) return c.json({ error: "windows_release_artifact_mirror_conflict" }, 409, noStoreHeaders());
    const queued = ledger.filter((row) => row.status !== "copied" && row.status !== "failed" && (row.last_queued_at === null || row.last_queued_at < now - WINDOWS_ARTIFACT_LEASE_SECONDS));
    await Promise.all(queued.map(async (row) => {
      const spec = specs.find((entry) => entry.assetName === row.asset_name)!;
      const payload = artifactPayload(spec, item.review_id, item.source_fingerprint, item.plan_hash);
      await c.env.JOBS.send({ jobId: windowsArtifactJobId(payload), uid: WINDOWS_ARTIFACT_SYSTEM_UID, kind: WINDOWS_ARTIFACT_MIRROR_KIND, payload });
      await c.env.APP_DB.prepare("UPDATE cf_windows_release_artifacts SET last_queued_at = ?, last_error = NULL, updated_at = ? WHERE release_id = ? AND asset_name = ? AND status IN ('queued', 'copying')").bind(now, now, row.release_id, row.asset_name).run();
    }));
    return c.json({ status: queued.length ? "queued" : "copied", release_id: plan.release.release_id, artifact_count: specs.length, queued_count: queued.length, copied_count: ledger.filter((row) => row.status === "copied").length, artifacts: ledger.map((row) => ({ asset_name: row.asset_name, status: row.status })) }, queued.length ? 202 : 200, noStoreHeaders());
  } catch {
    try { await c.env.APP_DB.prepare("UPDATE cf_windows_release_artifacts SET last_error = 'queue unavailable', updated_at = ? WHERE release_id = ? AND status = 'queued'").bind(now, plan.release.release_id).run(); } catch { /* durable ledger remains retryable */ }
    return c.json({ error: "windows_release_artifact_mirror_unavailable" }, 503, noStoreHeaders());
  }
}

function allowedSourceUrl(value: string, allowSignedQuery = false): URL | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.hash || !SOURCE_HOSTS.has(url.hostname.toLowerCase())) return null;
    if (url.search) {
      if (!allowSignedQuery || url.hostname.toLowerCase() === "github.com" || url.search.length > 4_096) return null;
      for (const key of url.searchParams.keys()) if (!/^X-Amz-(Algorithm|Credential|Date|Expires|SignedHeaders|Signature|Security-Token)$/.test(key)) return null;
    }
    return url;
  } catch { return null; }
}

async function fetchArtifact(sourceUrl: string): Promise<Response> {
  const initial = allowedSourceUrl(sourceUrl);
  if (!initial) throw new WindowsArtifactMirrorError("invalid_artifact_source", false);
  let current = initial;
  for (let attempt = 0; attempt <= WINDOWS_ARTIFACT_MAX_REDIRECTS; attempt += 1) {
    let response: Response;
    try { response = await fetch(current.toString(), { redirect: "manual" }); } catch { throw new WindowsArtifactMirrorError("artifact_source_unavailable", true); }
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      if (attempt === WINDOWS_ARTIFACT_MAX_REDIRECTS) throw new WindowsArtifactMirrorError("too_many_artifact_redirects", false);
      const location = response.headers.get("location");
      let next: URL | null = null;
      try { next = location ? new URL(location, current) : null; } catch { next = null; }
      const allowed = next ? allowedSourceUrl(next.toString(), true) : null;
      if (!allowed) throw new WindowsArtifactMirrorError("invalid_artifact_redirect", false);
      current = allowed;
      continue;
    }
    if (response.status === 404 || response.status === 410) throw new WindowsArtifactMirrorError("artifact_source_missing", false);
    if (response.status < 200 || response.status >= 300) throw new WindowsArtifactMirrorError("artifact_source_unavailable", true);
    if (!allowedSourceUrl(response.url || current.toString(), true)) throw new WindowsArtifactMirrorError("invalid_artifact_source", false);
    const declared = Number(response.headers.get("content-length"));
    if (Number.isFinite(declared) && (declared < 0 || declared > WINDOWS_ARTIFACT_MAX_BYTES)) throw new WindowsArtifactMirrorError("artifact_too_large", false);
    if (!response.body) throw new WindowsArtifactMirrorError("artifact_body_missing", true);
    return response;
  }
  throw new WindowsArtifactMirrorError("artifact_source_unavailable", true);
}

function boundedStream(source: ReadableStream<Uint8Array>): ReadableStream<Uint8Array> {
  const reader = source.getReader();
  let size = 0;
  return new ReadableStream({
    async pull(controller) {
      try {
        const item = await reader.read();
        if (item.done) return controller.close();
        size += item.value.byteLength;
        if (size > WINDOWS_ARTIFACT_MAX_BYTES) {
          await reader.cancel("artifact too large");
          return controller.error(new WindowsArtifactMirrorError("artifact_too_large", false));
        }
        controller.enqueue(item.value);
      } catch (error) { controller.error(error); }
    },
    async cancel(reason) { await reader.cancel(reason); },
  });
}

async function digestStream(source: ReadableStream<Uint8Array>): Promise<{ digest: string; size: number }> {
  const hash = sha256.create();
  const reader = source.getReader();
  let size = 0;
  try {
    while (true) {
      const item = await reader.read();
      if (item.done) break;
      size += item.value.byteLength;
      if (size > WINDOWS_ARTIFACT_MAX_BYTES) throw new WindowsArtifactMirrorError("artifact_too_large", false);
      hash.update(item.value);
    }
  } finally { reader.releaseLock(); }
  return { digest: bytesToHex(hash.digest()), size };
}

function payloadMatches(row: ArtifactRow, payload: ArtifactPayload): boolean {
  return row.release_id === payload.releaseId && row.asset_name === payload.assetName && row.review_id === payload.reviewId && row.source_fingerprint === payload.sourceFingerprint && row.plan_hash === payload.planHash && row.source_url === payload.sourceUrl && row.object_key === payload.objectKey && row.expected_sha256 === payload.expectedSha256 && row.content_type === payload.contentType;
}

async function markFailed(env: JobsEnv, payload: ArtifactPayload, code: string, now: number): Promise<void> {
  await env.APP_DB.prepare("UPDATE cf_windows_release_artifacts SET status = 'failed', last_error = ?, updated_at = ? WHERE release_id = ? AND asset_name = ?").bind(code, now, payload.releaseId, payload.assetName).run();
}

async function retryArtifact(message: WindowsMirrorMessage, env: JobsEnv, payload: ArtifactPayload, code: string, now: number): Promise<void> {
  if (message.attempts >= WINDOWS_ARTIFACT_MAX_ATTEMPTS) {
    await markFailed(env, payload, code, now);
    message.ack();
    return;
  }
  await env.APP_DB.prepare("UPDATE cf_windows_release_artifacts SET status = 'queued', last_error = ?, updated_at = ? WHERE release_id = ? AND asset_name = ? AND status = 'copying'").bind(code, now, payload.releaseId, payload.assetName).run();
  message.retry({ delaySeconds: WINDOWS_ARTIFACT_RETRY_SECONDS });
}

export async function processWindowsReleaseArtifactMessage(message: Message<JobMessage>, env: JobsEnv): Promise<void> {
  const mirrorMessage = message as WindowsMirrorMessage;
  const payloadValue = objectValue(message.body.payload);
  const payload = payloadValue && ASSET_KEYS.includes(payloadValue.assetKey as AssetKey) && typeof payloadValue.releaseId === "string" && typeof payloadValue.assetName === "string" && typeof payloadValue.sourceUrl === "string" && typeof payloadValue.objectKey === "string" && typeof payloadValue.expectedSha256 === "string" && typeof payloadValue.contentType === "string" && typeof payloadValue.reviewId === "string" && typeof payloadValue.sourceFingerprint === "string" && typeof payloadValue.planHash === "string" ? payloadValue as unknown as ArtifactPayload : null;
  const now = Math.floor(Date.now() / 1_000);
  if (!payload || message.body.uid !== WINDOWS_ARTIFACT_SYSTEM_UID || !SHA256.test(payload.expectedSha256) || !SHA256.test(payload.sourceFingerprint) || !SHA256.test(payload.planHash) || !validUuid(payload.reviewId)) { message.ack(); return; }
  const row = await env.APP_DB.prepare("SELECT release_id, asset_name, review_id, source_fingerprint, plan_hash, source_url, object_key, expected_sha256, content_type, status, attempts, last_queued_at FROM cf_windows_release_artifacts WHERE release_id = ? AND asset_name = ? LIMIT 1").bind(payload.releaseId, payload.assetName).first<ArtifactRow>();
  if (!row || !payloadMatches(row, payload)) { message.ack(); return; }
  if (row.status === "failed") { message.ack(); return; }
  if (!env.DESKTOP_UPDATES) { await retryArtifact(mirrorMessage, env, payload, "artifact_storage_unavailable", now); return; }
  let existing: R2Object | null;
  try { existing = await env.DESKTOP_UPDATES.head(payload.objectKey); } catch { await retryArtifact(mirrorMessage, env, payload, "artifact_storage_unavailable", now); return; }
  if (existing) {
    const metadata = existing.customMetadata || {};
    if (metadata.release_id === payload.releaseId && metadata.asset_name === payload.assetName && metadata.sha256 === payload.expectedSha256 && metadata.review_id === payload.reviewId && metadata.source_fingerprint === payload.sourceFingerprint && metadata.plan_hash === payload.planHash && existing.size >= 0) {
      await env.APP_DB.prepare("UPDATE cf_windows_release_artifacts SET status = 'copied', size_bytes = ?, last_error = NULL, copied_at = ?, updated_at = ? WHERE release_id = ? AND asset_name = ? AND status IN ('queued', 'copying')").bind(existing.size, now, now, payload.releaseId, payload.assetName).run();
      message.ack();
      return;
    }
    await markFailed(env, payload, "artifact_object_conflict", now);
    message.ack();
    return;
  }
  const claimed = await env.APP_DB.prepare("UPDATE cf_windows_release_artifacts SET status = 'copying', attempts = attempts + 1, updated_at = ? WHERE release_id = ? AND asset_name = ? AND (status = 'queued' OR (status = 'copying' AND updated_at <= ?))").bind(now, payload.releaseId, payload.assetName, now - WINDOWS_ARTIFACT_LEASE_SECONDS).run();
  if (Number(claimed?.meta?.changes || 0) !== 1) { message.ack(); return; }
  let response: Response;
  try { response = await fetchArtifact(payload.sourceUrl); } catch (error) {
    if (error instanceof WindowsArtifactMirrorError && !error.retryable) { await markFailed(env, payload, error.code, now); message.ack(); return; }
    await retryArtifact(mirrorMessage, env, payload, error instanceof WindowsArtifactMirrorError ? error.code : "artifact_source_unavailable", now);
    return;
  }
  const [putBody, digestBody] = response.body!.tee();
  const [putResult, digestResult] = await Promise.allSettled([
    env.DESKTOP_UPDATES.put(payload.objectKey, boundedStream(putBody), { httpMetadata: { contentType: payload.contentType }, customMetadata: { release_id: payload.releaseId, asset_name: payload.assetName, sha256: payload.expectedSha256, review_id: payload.reviewId, source_fingerprint: payload.sourceFingerprint, plan_hash: payload.planHash } }),
    digestStream(boundedStream(digestBody)),
  ]);
  if (putResult.status === "rejected" || digestResult.status === "rejected") {
    try { await env.DESKTOP_UPDATES.delete(payload.objectKey); } catch { /* retry fails closed on a stale object */ }
    const error = digestResult.status === "rejected" ? digestResult.reason : putResult.status === "rejected" ? putResult.reason : new WindowsArtifactMirrorError("artifact_storage_unavailable", true);
    if (error instanceof WindowsArtifactMirrorError && !error.retryable) { await markFailed(env, payload, error.code, now); message.ack(); return; }
    await retryArtifact(mirrorMessage, env, payload, "artifact_storage_unavailable", now);
    return;
  }
  if (digestResult.value.digest !== payload.expectedSha256) {
    try { await env.DESKTOP_UPDATES.delete(payload.objectKey); } catch { /* failed marker is authoritative */ }
    await markFailed(env, payload, "artifact_digest_mismatch", now);
    message.ack();
    return;
  }
  const head = await env.DESKTOP_UPDATES.head(payload.objectKey);
  const metadata = head?.customMetadata || {};
  if (!head || metadata.sha256 !== payload.expectedSha256 || metadata.release_id !== payload.releaseId || metadata.asset_name !== payload.assetName || metadata.review_id !== payload.reviewId || metadata.source_fingerprint !== payload.sourceFingerprint || metadata.plan_hash !== payload.planHash || head.size !== digestResult.value.size) {
    try { await env.DESKTOP_UPDATES.delete(payload.objectKey); } catch { /* retain failed marker */ }
    await markFailed(env, payload, "artifact_storage_verification_failed", now);
    message.ack();
    return;
  }
  await env.APP_DB.prepare("UPDATE cf_windows_release_artifacts SET status = 'copied', size_bytes = ?, last_error = NULL, copied_at = ?, updated_at = ? WHERE release_id = ? AND asset_name = ? AND status IN ('queued', 'copying')").bind(digestResult.value.size, now, now, payload.releaseId, payload.assetName).run();
  message.ack();
}

export function registerWindowsReleaseHistoryRoutes(app: Hono<{ Bindings: JobsEnv }>): void {
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
  app.post(`${REVIEW_PATH}/:reviewId/artifacts/apply`, async (c) => {
    const denied = gate(c);
    if (denied) return denied;
    return applyArtifacts(c);
  });
}

export const windowsReleaseHistoryContract = {
  reviewPath: REVIEW_PATH,
  reviewTtlSeconds: REVIEW_TTL_SECONDS,
  maxBodyBytes: MAX_BODY_BYTES,
};
