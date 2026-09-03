import type { JobsEnv } from "./env";

const APP_ID_MAX_LENGTH = 256;
const UID_MAX_LENGTH = 256;
const LOGO_VERSION =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function validSegment(value: string, maximum: number) {
  return (
    value.length > 0 &&
    value.length <= maximum &&
    !value.includes("/") &&
    !value.includes("\\")
  );
}

export function appLogoObjectKey(
  ownerUid: string,
  appId: string,
  version: string,
) {
  if (
    !validSegment(ownerUid, UID_MAX_LENGTH) ||
    !validSegment(appId, APP_ID_MAX_LENGTH) ||
    !LOGO_VERSION.test(version)
  ) {
    throw new Error("invalid app logo identity");
  }
  return `cf-app-logos/${ownerUid}/${appId}/${version}`;
}

export function appLogoUrl(env: JobsEnv, appId: string, version: string) {
  const raw = env.PUBLIC_API_BASE_URL?.trim();
  if (!raw) throw new Error("public API URL unavailable");
  const base = new URL(raw);
  if (base.protocol !== "https:" || base.username || base.password) {
    throw new Error("public API URL unavailable");
  }
  return `${base.toString().replace(/\/$/, "")}/v1/apps/${encodeURIComponent(appId)}/logo/${version}`;
}

export function appLogoObjectKeyFromPayload(
  rawPayload: string,
  ownerUid: string,
  appId: string,
): string | null {
  if (new TextEncoder().encode(rawPayload).byteLength > 500_000) {
    throw new Error("app catalog payload is invalid");
  }
  const payload = JSON.parse(rawPayload) as unknown;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("app catalog payload is invalid");
  }
  const image = (payload as Record<string, unknown>).image;
  if (typeof image !== "string" || !image) return null;
  let pathname: string;
  try {
    pathname = new URL(image).pathname;
  } catch {
    return null;
  }
  const prefix = `/v1/apps/${encodeURIComponent(appId)}/logo/`;
  if (!pathname.startsWith(prefix)) return null;
  const version = pathname.slice(prefix.length);
  if (version.includes("/")) return null;
  return appLogoObjectKey(ownerUid, appId, version);
}
