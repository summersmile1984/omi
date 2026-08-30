import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobsEnv } from "./env";

const MAX_HANDLE_LENGTH = 128;
const MAX_PROVIDER_RESPONSE_BYTES = 512_000;
const RAPID_API_HOST_PATTERN = /^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$/;

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;
type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export type TwitterProfileDependencies = Readonly<{
  fetchImpl?: FetchLike;
}>;

type TwitterProfilePayload = {
  name: string;
  profile: string;
  rest_id: string;
  avatar: string;
  desc: string;
  friends: number;
  sub_count: number;
  id: string;
  status: string;
};

class TwitterProfileError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function boundedString(value: unknown, maximum: number): string {
  return typeof value === "string" ? value.trim().slice(0, maximum) : "";
}

function nonNegativeInteger(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : 0;
}

function rapidApiConfiguration(env: JobsEnv) {
  const host = env.RAPID_API_HOST?.trim().toLowerCase();
  const key = env.RAPID_API_KEY?.trim();
  if (!host || !key || !RAPID_API_HOST_PATTERN.test(host)) return null;
  return { host, key };
}

function normalizeHandle(value: string | undefined): string | null {
  const handle = (value || "").trim().replace(/^@/, "");
  if (!handle || handle.length > MAX_HANDLE_LENGTH || /[^a-zA-Z0-9_.-]/.test(handle)) return null;
  return handle;
}

async function providerJson(response: Response): Promise<Record<string, unknown>> {
  if (!response.ok) throw new TwitterProfileError(502, "Twitter profile unavailable");
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength > MAX_PROVIDER_RESPONSE_BYTES) {
    throw new TwitterProfileError(502, "Twitter profile response too large");
  }
  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new TwitterProfileError(502, "Twitter profile response invalid");
  }
  const object = objectValue(payload);
  if (!object || object.status === "error") {
    throw new TwitterProfileError(502, "Twitter profile unavailable");
  }
  return object;
}

async function fetchTwitterProfile(
  env: JobsEnv,
  handle: string,
  dependencies: TwitterProfileDependencies,
): Promise<TwitterProfilePayload> {
  const configuration = rapidApiConfiguration(env);
  if (!configuration) throw new TwitterProfileError(503, "Twitter profile is not configured");
  const url = new URL(`https://${configuration.host}/screenname.php`);
  url.searchParams.set("screenname", handle);
  const response = await (dependencies.fetchImpl || fetch)(url, {
    headers: {
      "x-rapidapi-key": configuration.key,
      "x-rapidapi-host": configuration.host,
      accept: "application/json",
    },
    signal: AbortSignal.timeout(20_000),
  });
  const payload = await providerJson(response);
  return {
    name: boundedString(payload.name, 512),
    profile: boundedString(payload.profile, MAX_HANDLE_LENGTH),
    rest_id: boundedString(payload.rest_id, 256),
    avatar: boundedString(payload.avatar, 2_048).replace("_normal", ""),
    desc: boundedString(payload.desc, 4_000),
    friends: nonNegativeInteger(payload.friends),
    sub_count: nonNegativeInteger(payload.sub_count),
    id: boundedString(payload.id, 256),
    // The legacy Pydantic model defaults a successful response without a
    // status field to "error"; preserve that wire contract.
    status: boundedString(payload.status, 64) || "error",
  };
}

function personaFromRow(row: Record<string, unknown> | null) {
  if (!row || typeof row.data_json !== "string") return null;
  try {
    const data = objectValue(JSON.parse(row.data_json));
    if (!data || !Array.isArray(data.capabilities) || !data.capabilities.includes("persona")) return null;
    const id = boundedString(row.id, 256);
    const username = boundedString(data.username, 256);
    return id && username ? { persona_id: id, persona_username: username } : null;
  } catch {
    return null;
  }
}

async function findPersona(env: JobsEnv, uid: string, handle: string) {
  const owned = await env.APP_DB.prepare(
    "SELECT id, data_json FROM cf_app_catalog WHERE owner_uid = ? AND disabled = 0 ORDER BY updated_at DESC, id DESC LIMIT 25",
  )
    .bind(uid)
    .all<Record<string, unknown>>();
  for (const row of owned.results || []) {
    const persona = personaFromRow(row);
    if (persona) return persona;
  }
  const matching = await env.APP_DB.prepare(
    "SELECT id, data_json FROM cf_app_catalog WHERE disabled = 0 AND json_extract(data_json, '$.twitter.username') = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
  )
    .bind(handle)
    .first<Record<string, unknown>>();
  return personaFromRow(matching);
}

function errorResponse(c: JobsContext, error: unknown) {
  if (error instanceof TwitterProfileError) return c.json({ detail: error.message }, error.status as 400);
  return c.json({ detail: "Twitter profile unavailable" }, 503);
}

export function registerTwitterProfileRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
  dependencies: TwitterProfileDependencies = {},
) {
  app.get("/v1/personas/twitter/profile", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    const handle = normalizeHandle(c.req.query("handle"));
    if (!handle) return c.json({ detail: "Invalid Twitter handle" }, 422);
    try {
      const profile = await fetchTwitterProfile(c.env, handle, dependencies);
      const persona = await findPersona(c.env, context.uid, handle);
      return c.json(persona ? { ...profile, ...persona } : profile);
    } catch (error) {
      return errorResponse(c, error);
    }
  });
}
