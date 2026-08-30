import type { JobsEnv } from "./env";

const THUMBNAIL_ID_MAX_LENGTH = 128;

function validThumbnailId(value: string) {
  return (
    value.length > 0 &&
    value.length <= THUMBNAIL_ID_MAX_LENGTH &&
    /^[A-Za-z0-9_-]+$/.test(value)
  );
}

export function appThumbnailObjectKey(thumbnailId: string) {
  if (!validThumbnailId(thumbnailId)) {
    throw new Error("invalid app thumbnail identity");
  }
  return `cf-app-thumbnails/${thumbnailId}.jpg`;
}

export function appThumbnailUrl(env: JobsEnv, thumbnailId: string) {
  const raw = env.PUBLIC_API_BASE_URL?.trim();
  if (!raw) throw new Error("public API URL unavailable");
  const base = new URL(raw);
  if (base.protocol !== "https:" || base.username || base.password) {
    throw new Error("public API URL unavailable");
  }
  if (!validThumbnailId(thumbnailId)) {
    throw new Error("invalid app thumbnail identity");
  }
  return `${base.toString().replace(/\/$/, "")}/v1/app/thumbnails/${encodeURIComponent(thumbnailId)}.jpg`;
}

export function thumbnailIdFromPath(value: string) {
  const match = /^([A-Za-z0-9_-]{1,128})\.jpg$/.exec(value);
  return match?.[1] || null;
}
