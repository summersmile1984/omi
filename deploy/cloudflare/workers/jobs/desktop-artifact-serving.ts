import type { Context, Hono } from "hono";
import type { JobsEnv } from "./env";

// Public download surface for mirrored desktop release artifacts. Bytes are
// served only for ledger rows in status='copied' — the mirror jobs already
// verified size and SHA-256 before that transition — so this route can never
// invent an object key or serve an unverified upload. Artifacts are immutable
// once copied (the ledger key is release_id + asset_name and the object is
// digest-verified), which is what makes the long-lived immutable cache-control
// safe: Cloudflare's CDN caches the response at the edge and R2 egress is
// free either way.
const MAX_ID_LENGTH = 256;
const ARTIFACT_CACHE_CONTROL = "public, max-age=31536000, immutable";

type ArtifactRow = {
  object_key: string;
  content_type: string | null;
  size_bytes: number;
  expected_sha256: string;
};

function boundedSegment(raw: string | undefined): string | null {
  if (!raw) return null;
  let value: string;
  try {
    value = decodeURIComponent(raw);
  } catch {
    return null;
  }
  if (!value || value.length > MAX_ID_LENGTH || value.includes("/")) {
    return null;
  }
  return value;
}

async function copiedArtifact(
  env: JobsEnv,
  releaseId: string,
  assetName: string,
): Promise<ArtifactRow | null> {
  for (const table of [
    "cf_desktop_release_artifacts",
    "cf_windows_release_artifacts",
  ]) {
    const row = await env.APP_DB.prepare(
      `SELECT object_key, content_type, size_bytes, expected_sha256
       FROM ${table} WHERE release_id = ? AND asset_name = ? AND status = 'copied' LIMIT 1`,
    )
      .bind(releaseId, assetName)
      .first<ArtifactRow>();
    if (row) return row;
  }
  return null;
}

function parseRange(
  raw: string | null,
  size: number,
): { offset: number; length: number; start: number; end: number } | null {
  if (!raw || !raw.startsWith("bytes=") || raw.includes(",")) return null;
  const spec = raw.slice(6).trim();
  const separator = spec.indexOf("-");
  if (separator < 0) return null;
  const startRaw = spec.slice(0, separator);
  const endRaw = spec.slice(separator + 1);
  if (size <= 0) return null;
  if (!startRaw) {
    const suffix = Number(endRaw);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) return null;
    const start = Math.max(0, size - suffix);
    return { offset: start, length: size - start, start, end: size - 1 };
  }
  const start = Number(startRaw);
  if (!Number.isSafeInteger(start) || start < 0 || start >= size) return null;
  const end = endRaw ? Number(endRaw) : size - 1;
  if (!Number.isSafeInteger(end) || end < start) return null;
  const bounded = Math.min(end, size - 1);
  return {
    offset: start,
    length: bounded - start + 1,
    start,
    end: bounded,
  };
}

async function serveDesktopArtifact(c: Context<{ Bindings: JobsEnv }>) {
  const releaseId = boundedSegment(c.req.param("releaseId"));
  const assetName = boundedSegment(c.req.param("assetName"));
  if (!releaseId || !assetName) {
    return c.json({ error: "artifact not found" }, 404);
  }
  const bucket = c.env.DESKTOP_UPDATES;
  if (!bucket) {
    return c.json(
      { error: "artifact_storage_unavailable" },
      503,
      { "cache-control": "no-store" },
    );
  }
  let row: ArtifactRow | null;
  try {
    row = await copiedArtifact(c.env, releaseId, assetName);
  } catch {
    return c.json(
      { error: "artifact_storage_unavailable" },
      503,
      { "cache-control": "no-store" },
    );
  }
  if (!row) return c.json({ error: "artifact not found" }, 404);
  const size = Number(row.size_bytes);
  const range = parseRange(c.req.header("range") ?? null, size);
  if (c.req.header("range") && !range) {
    return new Response(null, {
      status: 416,
      headers: {
        "content-range": `bytes */${size}`,
        "accept-ranges": "bytes",
      },
    });
  }
  const object = await (range
    ? bucket.get(row.object_key, {
        range: { offset: range.offset, length: range.length },
      })
    : bucket.get(row.object_key));
  if (!object || !object.body) {
    // The ledger says copied but the object is gone: fail loudly instead of
    // redirecting to an unverified source.
    return c.json(
      { error: "artifact_storage_unavailable" },
      503,
      { "cache-control": "no-store" },
    );
  }
  const headers: Record<string, string> = {
    "content-type": row.content_type || "application/octet-stream",
    "accept-ranges": "bytes",
    "cache-control": ARTIFACT_CACHE_CONTROL,
    etag: `"${row.expected_sha256.replace(/^sha256:/, "")}"`,
  };
  if (range) {
    headers["content-range"] = `bytes ${range.start}-${range.end}/${size}`;
    headers["content-length"] = String(range.length);
    return new Response(object.body, { status: 206, headers });
  }
  headers["content-length"] = String(size);
  return new Response(object.body, { status: 200, headers });
}

export function registerDesktopArtifactServingRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
) {
  app.get("/v2/desktop/artifacts/:releaseId/:assetName", serveDesktopArtifact);
}
