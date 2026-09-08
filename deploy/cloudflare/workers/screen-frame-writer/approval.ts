/** Internal capability for exact approved JPEGs; never a client response. */
export type FrameMetadata = {
  captured_at: string;
  caption: string;
  labels: string[];
  source_badge: "code" | "browser" | "document" | "slides" | "product" | null;
  banner_suitability: number;
  width: number;
  height: number;
  ground: { stops: [string, string]; is_neutral: boolean };
};

export type Approval = {
  version: 1;
  iss: "omi-screen-frame-adjudicator";
  aud: "omi-screen-frame-writer";
  jti: string;
  uid: string;
  conversation_id: string;
  attempt_id: string;
  epoch: number;
  purpose: "meeting_note_v1";
  retention: "with_subject";
  decision: "approved_clean";
  model: "@cf/qwen/qwen3.8-27b";
  policy_version: string;
  prompt_version: string;
  issued_at: number;
  expires_at: number;
  canonical_sha256: string;
  thumbnail_sha256: string;
  metadata: FrameMetadata;
};

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256 = /^[0-9a-f]{64}$/;
export const APPROVAL_PURPOSE = "screen-frame-approval-v1";
export const CONTENT_PURPOSE = "screen-frame-content-v1";
const encoder = new TextEncoder();

export function validUid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 256 &&
    !value.includes("/")
  );
}

export function bytes(value: unknown, limit: number): Uint8Array {
  if (
    typeof value !== "string" ||
    value.length > Math.ceil(limit / 3) * 4 ||
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(
      value
    )
  )
    throw new Error("invalid image encoding");
  const data = Uint8Array.from(atob(value), (c) => c.charCodeAt(0));
  if (!data.length || data.byteLength > limit)
    throw new Error("invalid image size");
  return data;
}

export async function sha256(value: Uint8Array): Promise<string> {
  return Array.from(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", new Uint8Array(value))
    ),
    (b) => b.toString(16).padStart(2, "0")
  ).join("");
}

export async function verifyToken(
  token: unknown,
  secret: unknown,
  purpose: string
): Promise<Record<string, unknown>> {
  if (typeof secret !== "string" || secret.length < 32)
    throw new Error("signing unavailable");
  if (typeof token !== "string" || token.length > 24000)
    throw new Error("invalid approval");
  const [prefix, encoded, signature, extra] = token.split(".");
  if (
    prefix !== purpose ||
    !encoded ||
    !signature ||
    extra !== undefined ||
    !/^[A-Za-z0-9_-]+$/.test(encoded) ||
    !/^[A-Za-z0-9_-]+$/.test(signature)
  )
    throw new Error("invalid approval");
  const decode = (s: string) =>
    Uint8Array.from(
      atob(
        s.replace(/-/g, "+").replace(/_/g, "/") +
          "=".repeat((4 - (s.length % 4)) % 4)
      ),
      (c) => c.charCodeAt(0)
    );
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"]
  );
  if (
    !(await crypto.subtle.verify(
      "HMAC",
      key,
      decode(signature),
      encoder.encode(`${prefix}.${encoded}`)
    ))
  )
    throw new Error("invalid approval");
  const payload: unknown = JSON.parse(
    new TextDecoder("utf-8", { fatal: true }).decode(decode(encoded))
  );
  if (!payload || typeof payload !== "object" || Array.isArray(payload))
    throw new Error("invalid approval");
  return payload as Record<string, unknown>;
}

export async function verifyApproval(
  token: unknown,
  secret: unknown,
  now: number
): Promise<Approval> {
  const value = await verifyToken(token, secret, APPROVAL_PURPOSE);
  const p = value as unknown as Approval;
  const m = p.metadata;
  if (
    p.version !== 1 ||
    p.iss !== "omi-screen-frame-adjudicator" ||
    p.aud !== "omi-screen-frame-writer" ||
    !UUID.test(p.jti) ||
    !validUid(p.uid) ||
    typeof p.conversation_id !== "string" ||
    !p.conversation_id ||
    p.conversation_id.length > 256 ||
    !UUID.test(p.attempt_id) ||
    !Number.isSafeInteger(p.epoch) ||
    p.epoch < 0 ||
    p.purpose !== "meeting_note_v1" ||
    p.retention !== "with_subject" ||
    p.decision !== "approved_clean" ||
    p.model !== "@cf/qwen/qwen3.8-27b" ||
    typeof p.policy_version !== "string" ||
    !p.policy_version ||
    p.policy_version.length > 128 ||
    typeof p.prompt_version !== "string" ||
    !p.prompt_version ||
    p.prompt_version.length > 128 ||
    !Number.isSafeInteger(p.issued_at) ||
    !Number.isSafeInteger(p.expires_at) ||
    p.issued_at > now + 30 ||
    p.expires_at <= now ||
    p.expires_at <= p.issued_at ||
    p.expires_at - p.issued_at > 600 ||
    !SHA256.test(p.canonical_sha256) ||
    !SHA256.test(p.thumbnail_sha256) ||
    !m ||
    typeof m !== "object" ||
    typeof m.captured_at !== "string" ||
    !Number.isFinite(Date.parse(m.captured_at)) ||
    typeof m.caption !== "string" ||
    Array.from(m.caption).length > 160 ||
    !Array.isArray(m.labels) ||
    m.labels.length > 8 ||
    !m.labels.every((s) => typeof s === "string") ||
    ![null, "code", "browser", "document", "slides", "product"].includes(
      m.source_badge
    ) ||
    typeof m.banner_suitability !== "number" ||
    !Number.isFinite(m.banner_suitability) ||
    m.banner_suitability < 0 ||
    m.banner_suitability > 1 ||
    !Number.isInteger(m.width) ||
    m.width < 1 ||
    m.width > 1600 ||
    !Number.isInteger(m.height) ||
    m.height < 1 ||
    m.height > 1600 ||
    !m.ground ||
    !Array.isArray(m.ground.stops) ||
    m.ground.stops.length !== 2 ||
    !m.ground.stops.every((s) => /^#[0-9a-f]{6}$/i.test(s)) ||
    typeof m.ground.is_neutral !== "boolean" ||
    JSON.stringify(m).length > 8192
  )
    throw new Error("invalid approval claims");
  return p;
}

export function objectKeys(uid: string, jti: string): [string, string] {
  if (!validUid(uid) || !UUID.test(jti))
    throw new Error("invalid object owner");
  const prefix = `${encodeURIComponent(uid)}/${jti}`;
  return [`${prefix}/frame.jpg`, `${prefix}/thumbnail.jpg`];
}
