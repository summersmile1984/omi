import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import type { Message } from "@cloudflare/workers-types";
import type { JobMessage, JobsEnv } from "./env";

/**
 * R2 transfer boundary for reviewed desktop release artifacts.
 *
 * The source URL is still the immutable GitHub release until the release
 * pipeline is cut over.  The worker only follows GitHub's signed-artifact CDN
 * redirect hosts and writes an object after hashing the complete response.
 * Metadata is intentionally content-bound so a retry can never overwrite a
 * different artifact at the same key.
 */

export const DESKTOP_ARTIFACT_MIRROR_KIND =
  "desktop_release_artifact_mirror" as const;
export const DESKTOP_ARTIFACT_SYSTEM_UID = "system:desktop-release-artifact";
export const DESKTOP_ARTIFACT_MAX_BYTES = 1_073_741_824;
export const DESKTOP_ARTIFACT_MAX_REDIRECTS = 3;
export const DESKTOP_ARTIFACT_MAX_ATTEMPTS = 3;
export const DESKTOP_ARTIFACT_LEASE_SECONDS = 15 * 60;
export const DESKTOP_ARTIFACT_RETRY_SECONDS = 30;

const RELEASE_ID = /^v[0-9]+\.[0-9]+(?:\.[0-9]+)?\+[1-9][0-9]*-macos$/;
const SHA256 = /^sha256:[0-9a-f]{64}$/;
const MANIFEST_SHA256 = /^[0-9a-f]{64}$/;
const ASSET_NAME = /^(?:Omi\.zip|omi\.dmg|qualification-evidence-[^/]+\.json|desktop-smoke-result(?:-beta)?\.json)$/;
const SOURCE_HOSTS = new Set([
  "github.com",
  "release-assets.githubusercontent.com",
  "objects.githubusercontent.com",
  "githubusercontent.com",
]);
const CONTENT_TYPES: Record<string, string> = {
  "Omi.zip": "application/zip",
  "omi.dmg": "application/x-apple-diskimage",
};

type JsonObject = Record<string, unknown>;

export type DesktopArtifactSpec = {
  releaseId: string;
  assetName: string;
  sourceUrl: string;
  objectKey: string;
  expectedSha256: string;
  contentType: string;
};

type ArtifactRow = {
  release_id: string;
  asset_name: string;
  review_id: string;
  plan_hash: string;
  manifest_sha256: string;
  source_url: string;
  object_key: string;
  expected_sha256: string;
  content_type: string;
  size_bytes: number | null;
  status: "queued" | "copying" | "copied" | "failed";
  attempts: number;
  last_queued_at: number | null;
  last_error: string | null;
};

type ArtifactMessagePayload = {
  releaseId: string;
  assetName: string;
  sourceUrl: string;
  objectKey: string;
  expectedSha256: string;
  contentType: string;
  reviewId: string;
  manifestSha256: string;
};

type MirrorMessage = Message<JobMessage> & {
  body: JobMessage & { kind: typeof DESKTOP_ARTIFACT_MIRROR_KIND };
};

export class DesktopArtifactMirrorError extends Error {
  readonly retryable: boolean;
  readonly code: string;

  constructor(code: string, retryable: boolean) {
    super(code);
    this.name = "DesktopArtifactMirrorError";
    this.code = code;
    this.retryable = retryable;
  }
}

function objectValue(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}

function validReviewId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      value,
    )
  );
}

function canonicalSourceUrl(releaseId: string, assetName: string): string {
  return `https://github.com/BasedHardware/omi/releases/download/${releaseId}/${assetName}`;
}

function canonicalObjectKey(releaseId: string, assetName: string): string {
  return `desktop-releases/${releaseId}/${assetName}`;
}

function contentTypeFor(assetName: string): string {
  return CONTENT_TYPES[assetName] || "application/json";
}

function expectedDigest(manifest: JsonObject, assetName: string): string | null {
  const field =
    assetName === "Omi.zip"
      ? "zip_sha256"
      : assetName === "omi.dmg"
        ? "dmg_sha256"
        : "qualification_evidence_sha256";
  const value = manifest[field];
  return typeof value === "string" && SHA256.test(value) ? value : null;
}

function evidenceAssetName(manifest: JsonObject): string | null {
  const value = manifest.qualification_evidence_asset;
  return typeof value === "string" && ASSET_NAME.test(value) ? value : null;
}

/** Return the exact three assets represented by one validated macOS manifest. */
export function desktopArtifactSpecs(
  manifest: JsonObject,
): DesktopArtifactSpec[] {
  const releaseId = manifest.release_id;
  if (typeof releaseId !== "string" || !RELEASE_ID.test(releaseId))
    throw new DesktopArtifactMirrorError("invalid_release_id", false);
  const evidence = evidenceAssetName(manifest);
  if (!evidence) throw new DesktopArtifactMirrorError("invalid_evidence_asset", false);
  const entries: Array<[string, string, string]> = [
    ["Omi.zip", "zip_url", "zip_sha256"],
    ["omi.dmg", "dmg_url", "dmg_sha256"],
    [evidence, "", "qualification_evidence_sha256"],
  ];
  const specs = entries.map(([assetName, urlField, digestField]) => {
    const expectedSha256 = manifest[digestField];
    if (typeof expectedSha256 !== "string" || !SHA256.test(expectedSha256))
      throw new DesktopArtifactMirrorError("invalid_artifact_digest", false);
    const sourceUrl = urlField
      ? manifest[urlField]
      : canonicalSourceUrl(releaseId, assetName);
    if (
      typeof sourceUrl !== "string" ||
      sourceUrl !== canonicalSourceUrl(releaseId, assetName)
    ) {
      throw new DesktopArtifactMirrorError("invalid_artifact_source", false);
    }
    return {
      releaseId,
      assetName,
      sourceUrl,
      objectKey: canonicalObjectKey(releaseId, assetName),
      expectedSha256,
      contentType: contentTypeFor(assetName),
    };
  });
  if (
    specs.length !== 3 ||
    new Set(specs.map((spec) => spec.assetName)).size !== specs.length
  ) {
    throw new DesktopArtifactMirrorError("invalid_artifact_set", false);
  }
  return specs;
}

function parsePayload(value: unknown): ArtifactMessagePayload | null {
  const payload = objectValue(value);
  if (!payload || Object.keys(payload).some((key) => ![
    "releaseId",
    "assetName",
    "sourceUrl",
    "objectKey",
    "expectedSha256",
    "contentType",
    "reviewId",
    "manifestSha256",
  ].includes(key))) return null;
  const releaseId = payload.releaseId;
  const assetName = payload.assetName;
  const sourceUrl = payload.sourceUrl;
  const objectKey = payload.objectKey;
  const expectedSha256 = payload.expectedSha256;
  const contentType = payload.contentType;
  const reviewId = payload.reviewId;
  const manifestSha256 = payload.manifestSha256;
  if (
    typeof releaseId !== "string" || !RELEASE_ID.test(releaseId) ||
    typeof assetName !== "string" || !ASSET_NAME.test(assetName) ||
    typeof sourceUrl !== "string" || sourceUrl !== canonicalSourceUrl(releaseId, assetName) ||
    typeof objectKey !== "string" || objectKey !== canonicalObjectKey(releaseId, assetName) ||
    typeof expectedSha256 !== "string" || !SHA256.test(expectedSha256) ||
    typeof contentType !== "string" || contentType !== contentTypeFor(assetName) ||
    !validReviewId(reviewId) ||
    typeof manifestSha256 !== "string" || !MANIFEST_SHA256.test(manifestSha256)
  ) return null;
  return {
    releaseId,
    assetName,
    sourceUrl,
    objectKey,
    expectedSha256,
    contentType,
    reviewId,
    manifestSha256,
  };
}

function allowedSourceUrl(value: string, allowSignedQuery = false): URL | null {
  try {
    const url = new URL(value);
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.hash ||
      !SOURCE_HOSTS.has(url.hostname.toLowerCase())
    ) return null;
    if (url.search) {
      // GitHub's release download redirects carry an S3-style signature.  A
      // query is allowed only on the trusted CDN hosts, with bounded length
      // and the exact signing keys; the canonical source URL never has one.
      if (!allowSignedQuery || url.hostname.toLowerCase() === "github.com" || url.search.length > 4_096) return null;
      for (const key of url.searchParams.keys()) {
        if (!/^X-Amz-(Algorithm|Credential|Date|Expires|SignedHeaders|Signature|Security-Token)$/.test(key)) return null;
      }
    }
    return url;
  } catch {
    return null;
  }
}

async function fetchArtifact(sourceUrl: string): Promise<Response> {
  const initial = allowedSourceUrl(sourceUrl);
  if (!initial) throw new DesktopArtifactMirrorError("invalid_artifact_source", false);
  let current: URL = initial;
  for (let attempt = 0; attempt <= DESKTOP_ARTIFACT_MAX_REDIRECTS; attempt += 1) {
    let response: Response;
    try {
      response = await fetch(current.toString(), { redirect: "manual" });
    } catch {
      throw new DesktopArtifactMirrorError("artifact_source_unavailable", true);
    }
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      if (attempt === DESKTOP_ARTIFACT_MAX_REDIRECTS)
        throw new DesktopArtifactMirrorError("too_many_artifact_redirects", false);
      const location = response.headers.get("location");
      const redirectUrl = location ? new URL(location, current) : null;
      const next: URL | null = redirectUrl ? allowedSourceUrl(redirectUrl.toString(), true) : null;
      if (!next) throw new DesktopArtifactMirrorError("invalid_artifact_redirect", false);
      current = next;
      continue;
    }
    if (response.status === 404 || response.status === 410)
      throw new DesktopArtifactMirrorError("artifact_source_missing", false);
    if (response.status < 200 || response.status >= 300)
      throw new DesktopArtifactMirrorError("artifact_source_unavailable", true);
    const finalUrl = allowedSourceUrl(response.url || current.toString(), true);
    if (!finalUrl) throw new DesktopArtifactMirrorError("invalid_artifact_source", false);
    const declared = Number(response.headers.get("content-length"));
    if (Number.isFinite(declared) && (declared < 0 || declared > DESKTOP_ARTIFACT_MAX_BYTES))
      throw new DesktopArtifactMirrorError("artifact_too_large", false);
    if (!response.body) throw new DesktopArtifactMirrorError("artifact_body_missing", true);
    return response;
  }
  throw new DesktopArtifactMirrorError("artifact_source_unavailable", true);
}

function boundedStream(
  source: ReadableStream<Uint8Array>,
): ReadableStream<Uint8Array> {
  const reader = source.getReader();
  let size = 0;
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const item = await reader.read();
        if (item.done) {
          controller.close();
          return;
        }
        size += item.value.byteLength;
        if (size > DESKTOP_ARTIFACT_MAX_BYTES) {
          await reader.cancel("artifact too large");
          controller.error(new DesktopArtifactMirrorError("artifact_too_large", false));
          return;
        }
        controller.enqueue(item.value);
      } catch (error) {
        controller.error(error);
      }
    },
    async cancel(reason) {
      await reader.cancel(reason);
    },
  });
}

async function digestStream(
  source: ReadableStream<Uint8Array>,
): Promise<{ digest: string; size: number }> {
  const hash = sha256.create();
  const reader = source.getReader();
  let size = 0;
  try {
    while (true) {
      const item = await reader.read();
      if (item.done) break;
      size += item.value.byteLength;
      if (size > DESKTOP_ARTIFACT_MAX_BYTES)
        throw new DesktopArtifactMirrorError("artifact_too_large", false);
      hash.update(item.value);
    }
  } finally {
    reader.releaseLock();
  }
  return { digest: `sha256:${bytesToHex(hash.digest())}`, size };
}

function rowMatches(row: ArtifactRow, payload: ArtifactMessagePayload): boolean {
  return (
    row.release_id === payload.releaseId &&
    row.asset_name === payload.assetName &&
    row.review_id === payload.reviewId &&
    row.manifest_sha256 === payload.manifestSha256 &&
    row.source_url === payload.sourceUrl &&
    row.object_key === payload.objectKey &&
    row.expected_sha256 === payload.expectedSha256 &&
    row.content_type === payload.contentType
  );
}

async function markCopied(
  env: JobsEnv,
  payload: ArtifactMessagePayload,
  size: number,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_desktop_release_artifacts SET status = 'copied', size_bytes = ?, last_error = NULL, copied_at = ?, updated_at = ? WHERE release_id = ? AND asset_name = ? AND status IN ('queued', 'copying')",
  ).bind(size, now, now, payload.releaseId, payload.assetName).run();
}

async function markFailed(
  env: JobsEnv,
  payload: ArtifactMessagePayload,
  code: string,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_desktop_release_artifacts SET status = 'failed', last_error = ?, updated_at = ? WHERE release_id = ? AND asset_name = ?",
  ).bind(code, now, payload.releaseId, payload.assetName).run();
}

async function retryArtifact(
  message: MirrorMessage,
  env: JobsEnv,
  payload: ArtifactMessagePayload,
  code: string,
  now: number,
): Promise<void> {
  if (message.attempts >= DESKTOP_ARTIFACT_MAX_ATTEMPTS) {
    await markFailed(env, payload, code, now);
    message.ack();
    return;
  }
  await env.APP_DB.prepare(
    "UPDATE cf_desktop_release_artifacts SET status = 'queued', last_error = ?, updated_at = ? WHERE release_id = ? AND asset_name = ? AND status = 'copying'",
  ).bind(code, now, payload.releaseId, payload.assetName).run();
  message.retry({ delaySeconds: DESKTOP_ARTIFACT_RETRY_SECONDS });
}

/** Process one idempotent reviewed artifact transfer from the Jobs queue. */
export async function processDesktopReleaseArtifactMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  const mirrorMessage = message as MirrorMessage;
  const payload = parsePayload(message.body.payload);
  const now = Math.floor(Date.now() / 1_000);
  if (!payload || message.body.uid !== DESKTOP_ARTIFACT_SYSTEM_UID) {
    message.ack();
    return;
  }
  const row = await env.APP_DB.prepare(
    "SELECT release_id, asset_name, review_id, plan_hash, manifest_sha256, source_url, object_key, expected_sha256, content_type, size_bytes, status, attempts, last_error FROM cf_desktop_release_artifacts WHERE release_id = ? AND asset_name = ? LIMIT 1",
  ).bind(payload.releaseId, payload.assetName).first<ArtifactRow>();
  if (!row || !rowMatches(row, payload)) {
    message.ack();
    return;
  }
  if (row.status === "failed") {
    message.ack();
    return;
  }
  if (!env.DESKTOP_UPDATES) {
    await retryArtifact(mirrorMessage, env, payload, "artifact_storage_unavailable", now);
    return;
  }

  let existing: R2Object | null;
  try {
    existing = await env.DESKTOP_UPDATES.head(payload.objectKey);
  } catch {
    await retryArtifact(mirrorMessage, env, payload, "artifact_storage_unavailable", now);
    return;
  }
  if (existing) {
    const metadata = existing.customMetadata || {};
    if (
      metadata.release_id === payload.releaseId &&
      metadata.asset_name === payload.assetName &&
      metadata.sha256 === payload.expectedSha256 &&
      metadata.review_id === payload.reviewId &&
      metadata.manifest_sha256 === payload.manifestSha256 &&
      existing.size >= 0
    ) {
      await markCopied(env, payload, existing.size, now);
      message.ack();
      return;
    }
    await markFailed(env, payload, "artifact_object_conflict", now);
    message.ack();
    return;
  }

  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_desktop_release_artifacts SET status = 'copying', attempts = attempts + 1, updated_at = ? WHERE release_id = ? AND asset_name = ? AND (status = 'queued' OR (status = 'copying' AND updated_at <= ?))",
  ).bind(now, payload.releaseId, payload.assetName, now - DESKTOP_ARTIFACT_LEASE_SECONDS).run();
  if (Number(claimed?.meta?.changes || 0) !== 1) {
    message.ack();
    return;
  }

  let response: Response;
  try {
    response = await fetchArtifact(payload.sourceUrl);
  } catch (error) {
    if (error instanceof DesktopArtifactMirrorError && !error.retryable) {
      await markFailed(env, payload, error.code, now);
      message.ack();
      return;
    }
    await retryArtifact(
      mirrorMessage,
      env,
      payload,
      error instanceof DesktopArtifactMirrorError ? error.code : "artifact_source_unavailable",
      now,
    );
    return;
  }

  const [putBody, digestBody] = response.body!.tee();
  const [putResult, digestResult] = await Promise.allSettled([
    env.DESKTOP_UPDATES.put(
      payload.objectKey,
      boundedStream(putBody),
      {
        httpMetadata: { contentType: payload.contentType },
        customMetadata: {
          release_id: payload.releaseId,
          asset_name: payload.assetName,
          sha256: payload.expectedSha256,
          review_id: payload.reviewId,
          manifest_sha256: payload.manifestSha256,
        },
      },
    ),
    digestStream(boundedStream(digestBody)),
  ]);
  if (putResult.status === "rejected" || digestResult.status === "rejected") {
    try { await env.DESKTOP_UPDATES.delete(payload.objectKey); } catch { /* retry will fail closed on a stale object */ }
    const error = digestResult.status === "rejected"
      ? digestResult.reason
      : putResult.status === "rejected"
        ? putResult.reason
        : new DesktopArtifactMirrorError("artifact_storage_unavailable", true);
    if (error instanceof DesktopArtifactMirrorError && !error.retryable) {
      await markFailed(env, payload, error.code, now);
      message.ack();
      return;
    }
    await retryArtifact(mirrorMessage, env, payload, "artifact_storage_unavailable", now);
    return;
  }
  if (digestResult.value.digest !== payload.expectedSha256) {
    try { await env.DESKTOP_UPDATES.delete(payload.objectKey); } catch { /* retain failed marker */ }
    await markFailed(env, payload, "artifact_digest_mismatch", now);
    message.ack();
    return;
  }
  const head = await env.DESKTOP_UPDATES.head(payload.objectKey);
  const metadata = head?.customMetadata || {};
  if (
    !head ||
    metadata.sha256 !== payload.expectedSha256 ||
    metadata.release_id !== payload.releaseId ||
    metadata.asset_name !== payload.assetName ||
    metadata.review_id !== payload.reviewId ||
    metadata.manifest_sha256 !== payload.manifestSha256 ||
    head.size !== digestResult.value.size
  ) {
    try { await env.DESKTOP_UPDATES.delete(payload.objectKey); } catch { /* retain failed marker */ }
    await markFailed(env, payload, "artifact_storage_verification_failed", now);
    message.ack();
    return;
  }
  await markCopied(env, payload, digestResult.value.size, now);
  message.ack();
}

export function artifactJobId(payload: ArtifactMessagePayload): string {
  const bytes = new TextEncoder().encode(
    `${payload.releaseId}\0${payload.assetName}\0${payload.expectedSha256}\0${payload.manifestSha256}`,
  );
  const digest = sha256(bytes);
  return `desktop-artifact-${bytesToHex(digest).slice(0, 48)}`;
}

export function artifactPayload(spec: DesktopArtifactSpec, reviewId: string, manifestSha256: string): ArtifactMessagePayload {
  return {
    releaseId: spec.releaseId,
    assetName: spec.assetName,
    sourceUrl: spec.sourceUrl,
    objectKey: spec.objectKey,
    expectedSha256: spec.expectedSha256,
    contentType: spec.contentType,
    reviewId,
    manifestSha256,
  };
}
