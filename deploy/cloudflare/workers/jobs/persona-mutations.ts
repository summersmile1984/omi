import {
  FormDataParseError,
  MaxFileSizeExceededError,
  MaxFilesExceededError,
  MaxPartsExceededError,
  MaxTotalSizeExceededError,
  parseFormData,
  type FileUpload,
} from "@remix-run/form-data-parser";
import type { Context, Hono } from "hono";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
  type SignedAuthContext,
} from "../shared/auth-context";
import { recordFallback } from "../shared/fallback";
import { validAccountDeletionUid } from "./account-deletion-residual";
import { appLogoObjectKey, appLogoUrl } from "./app-logo";
import type { JobsEnv } from "./env";

const MAX_PERSONA_DATA_BYTES = 500_000;
const MAX_PERSONA_IMAGE_BYTES = 10 * 1_024 * 1_024;
const MAX_PERSONA_REQUEST_BYTES =
  MAX_PERSONA_DATA_BYTES + MAX_PERSONA_IMAGE_BYTES + 64_000;
const MAX_NAME_LENGTH = 160;
const MAX_USERNAME_LENGTH = 120;
const MAX_DESCRIPTION_LENGTH = 20_000;
const MAX_PROMPT_LENGTH = 100_000;
const MAX_CONTEXT_LENGTH = 20_000;
const PERSONA_MODEL = "@cf/meta/llama-3.2-3b-instruct";

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;
type JsonObject = Record<string, unknown>;

class PersonaMutationError extends Error {
  constructor(
    readonly status: 400 | 404 | 409 | 413 | 422 | 503,
    readonly detail: string,
  ) {
    super(detail);
  }
}

function objectValue(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}

function stringValue(
  value: unknown,
  name: string,
  maximum: number,
  required = false,
) {
  if (value === undefined || value === null) {
    if (required) throw new PersonaMutationError(422, `${name} is required`);
    return "";
  }
  if (typeof value !== "string")
    throw new PersonaMutationError(422, `${name} is invalid`);
  const result = value.trim();
  if ((required && !result) || result.length > maximum) {
    throw new PersonaMutationError(422, `${name} is invalid`);
  }
  return result;
}

function stringList(value: unknown, name: string) {
  if (!Array.isArray(value) || value.length > 20) {
    throw new PersonaMutationError(422, `${name} is invalid`);
  }
  const values = value.map((item) => {
    if (typeof item !== "string" || !item.trim() || item.length > 256) {
      throw new PersonaMutationError(422, `${name} is invalid`);
    }
    return item.trim();
  });
  return [...new Set(values)];
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value as JsonObject)
      .sort()
      .map(
        (key) =>
          `${JSON.stringify(key)}:${stableJson((value as JsonObject)[key])}`,
      )
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function hex(bytes: ArrayBuffer) {
  return [...new Uint8Array(bytes)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function imageType(bytes: Uint8Array, declared: string) {
  const png =
    bytes.length >= 8 &&
    [137, 80, 78, 71, 13, 10, 26, 10].every(
      (value, index) => bytes[index] === value,
    );
  const jpeg =
    bytes.length >= 3 &&
    bytes[0] === 255 &&
    bytes[1] === 216 &&
    bytes[2] === 255;
  const gif =
    bytes.length >= 6 &&
    new TextDecoder().decode(bytes.subarray(0, 6)).match(/^GIF8[79]a$/);
  const webp =
    bytes.length >= 12 &&
    new TextDecoder().decode(bytes.subarray(0, 4)) === "RIFF" &&
    new TextDecoder().decode(bytes.subarray(8, 12)) === "WEBP";
  const detected = png
    ? "image/png"
    : jpeg
      ? "image/jpeg"
      : gif
        ? "image/gif"
        : webp
          ? "image/webp"
          : null;
  if (
    !detected ||
    (declared &&
      declared !== detected &&
      declared !== "application/octet-stream")
  ) {
    throw new PersonaMutationError(422, "Persona image is invalid");
  }
  return detected;
}

function parseAiText(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(parseAiText).join("");
  if (value && typeof value === "object") {
    const object = value as JsonObject;
    for (const key of ["response", "text", "content"]) {
      if (key in object) return parseAiText(object[key]);
    }
  }
  return "";
}

function usernameBase(name: string) {
  const value = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "")
    .slice(0, MAX_USERNAME_LENGTH);
  return value || "mypersona";
}

function username(value: unknown, name: string) {
  const explicit = stringValue(value, "Username", MAX_USERNAME_LENGTH);
  const result = explicit || usernameBase(name);
  if (!/^[a-zA-Z0-9_.-]+$/.test(result)) {
    throw new PersonaMutationError(422, "Username is invalid");
  }
  return result;
}

async function authProfile(env: JobsEnv, context: SignedAuthContext) {
  const signed = await createSignedAuthContext(
    { uid: context.uid, authority: "internal", requestId: context.requestId },
    "auth",
    "GET",
    "/internal/profile",
    env.INTERNAL_ASSERTION_SECRET,
  );
  if (!signed)
    throw new PersonaMutationError(503, "Persona profile unavailable");
  const response = await env.AUTH.fetch(
    new Request("https://auth.internal/internal/profile", {
      headers: {
        [AUTH_CONTEXT_HEADER]: signed.encoded,
        [AUTH_SIGNATURE_HEADER]: signed.signature,
        "x-request-id": context.requestId,
      },
    }),
  );
  if (!response.ok) {
    await response.arrayBuffer();
    throw new PersonaMutationError(503, "Persona profile unavailable");
  }
  const profile = objectValue(await response.json());
  if (!profile || profile.uid !== context.uid)
    throw new PersonaMutationError(503, "Persona profile unavailable");
  return {
    name:
      typeof profile.name === "string"
        ? profile.name.trim().slice(0, MAX_NAME_LENGTH)
        : "",
    email:
      typeof profile.email === "string"
        ? profile.email.trim().slice(0, 512)
        : "",
  };
}

function personaPrompt(name: string, input: JsonObject) {
  const context = Object.entries(input)
    .filter(
      ([key, value]) =>
        !["id", "uid", "approved", "status", "image", "email"].includes(key) &&
        typeof value === "string",
    )
    .map(([key, value]) => `${key}: ${String(value).trim().slice(0, 2_000)}`)
    .filter((value) => value.length > 0)
    .join("\n");
  return (
    `You are ${name}, an authentic conversational persona. Never mention being an AI or a clone. ` +
    "Keep responses natural, concise, opinionated, and grounded in the supplied identity.\n" +
    (context || "No additional persona context was provided.")
  ).slice(0, MAX_CONTEXT_LENGTH);
}

async function generateDescription(
  env: JobsEnv,
  name: string,
  input: JsonObject,
) {
  if (!env.AI || typeof env.AI.run !== "function") {
    recordFallback({
      component: "other",
      from: "workers_ai_streaming",
      to: "none",
      reason: "dependency_unavailable",
      outcome: "exhausted",
    });
    throw new PersonaMutationError(503, "Persona description unavailable");
  }
  try {
    const result = await env.AI.run(PERSONA_MODEL, {
      messages: [
        {
          role: "system",
          content:
            "Write only a concise, engaging persona description, at most 250 characters.",
        },
        {
          role: "user",
          content: `Name: ${name}\nPersona data: ${stableJson(input)}`,
        },
      ],
      max_tokens: 96,
      temperature: 0.3,
    });
    const description = parseAiText(result)
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 250);
    if (!description) throw new Error("empty persona description");
    return description;
  } catch {
    recordFallback({
      component: "other",
      from: "workers_ai_streaming",
      to: "none",
      reason: "dependency_unavailable",
      outcome: "exhausted",
    });
    throw new PersonaMutationError(503, "Persona description unavailable");
  }
}

type ParsedPersonaMultipart = {
  input: JsonObject;
  file: { bytes: Uint8Array; contentType: string };
};

async function parsePersonaMultipart(
  request: Request,
): Promise<ParsedPersonaMultipart> {
  let input: JsonObject | null = null;
  let file: { bytes: Uint8Array; contentType: string } | null = null;
  try {
    await parseFormData(
      request,
      {
        maxFiles: 1,
        maxFileSize: MAX_PERSONA_IMAGE_BYTES,
        maxParts: 3,
        maxTotalSize: MAX_PERSONA_REQUEST_BYTES,
      },
      async (upload: FileUpload) => {
        if (upload.fieldName !== "file" || file)
          throw new PersonaMutationError(400, "Persona image field is invalid");
        const bytes = new Uint8Array(await upload.arrayBuffer());
        if (!bytes.length)
          throw new PersonaMutationError(422, "Persona image is empty");
        file = { bytes, contentType: imageType(bytes, upload.type) };
        return "persona-image";
      },
    ).then((form) => {
      const raw = form.get("persona_data");
      if (
        typeof raw !== "string" ||
        !raw ||
        new TextEncoder().encode(raw).byteLength > MAX_PERSONA_DATA_BYTES
      ) {
        throw new PersonaMutationError(400, "persona_data is invalid");
      }
      try {
        input = objectValue(JSON.parse(raw));
      } catch {
        input = null;
      }
      if (!input)
        throw new PersonaMutationError(400, "persona_data is invalid");
    });
  } catch (error) {
    if (
      error instanceof MaxFilesExceededError ||
      error instanceof MaxFileSizeExceededError ||
      error instanceof MaxTotalSizeExceededError
    ) {
      throw new PersonaMutationError(413, "Persona upload is too large");
    }
    if (
      error instanceof FormDataParseError ||
      error instanceof MaxPartsExceededError ||
      error instanceof SyntaxError
    ) {
      throw new PersonaMutationError(400, "Persona upload is invalid");
    }
    throw error;
  }
  if (!input || !file)
    throw new PersonaMutationError(422, "Persona image is required");
  return {
    input: input as JsonObject,
    file: file as { bytes: Uint8Array; contentType: string },
  };
}

function errorResponse(c: JobsContext, error: unknown) {
  if (error instanceof PersonaMutationError)
    return c.json({ detail: error.detail }, error.status);
  return c.json({ error: "persona_unavailable" }, 503);
}

async function createPersona(c: JobsContext, context: SignedAuthContext) {
  if (!validAccountDeletionUid(context.uid))
    throw new PersonaMutationError(422, "Persona owner is invalid");
  const parsed = await parsePersonaMultipart(c.req.raw);
  const input = parsed.input;
  const profile = await authProfile(c.env, context);
  const name = stringValue(
    input.name || profile.name || "",
    "Persona name",
    MAX_NAME_LENGTH,
    true,
  );
  const personaUsername = username(input.username, name);
  const connectedAccounts =
    input.connected_accounts === undefined || input.connected_accounts === null
      ? ["omi"]
      : stringList(input.connected_accounts, "Connected accounts");
  if (!connectedAccounts.includes("omi")) connectedAccounts.push("omi");
  if (input.private !== undefined && typeof input.private !== "boolean") {
    throw new PersonaMutationError(422, "Private flag is invalid");
  }
  const fingerprint = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(
      `${context.uid}\x1f${stableJson(input)}\x1f${hex(
        await crypto.subtle.digest(
          "SHA-256",
          parsed.file.bytes.buffer as ArrayBuffer,
        ),
      )}`,
    ),
  );
  const personaId = `cf_persona_${hex(fingerprint).slice(0, 32)}`;
  const existing = await c.env.APP_DB.prepare(
    "SELECT owner_uid, data_json FROM cf_app_catalog WHERE id = ? LIMIT 1",
  )
    .bind(personaId)
    .first<{ owner_uid: string | null; data_json: string }>();
  if (existing) {
    if (existing.owner_uid !== context.uid)
      throw new PersonaMutationError(409, "Persona already exists");
    let data: JsonObject;
    try {
      data = objectValue(JSON.parse(existing.data_json)) || {};
    } catch {
      throw new PersonaMutationError(503, "Persona unavailable");
    }
    if (
      Array.isArray(data.capabilities) &&
      data.capabilities.includes("persona")
    ) {
      return c.json({
        status: "ok",
        app_id: personaId,
        username: String(data.username || personaUsername),
      });
    }
    throw new PersonaMutationError(409, "Persona already exists");
  }
  const usernameRow = await c.env.APP_DB.prepare(
    "SELECT id, owner_uid FROM cf_app_catalog WHERE json_extract(data_json, '$.username') = ? LIMIT 1",
  )
    .bind(personaUsername)
    .first<{ id: string; owner_uid: string | null }>();
  if (usernameRow)
    throw new PersonaMutationError(409, "Persona username is already taken");

  const version = crypto.randomUUID();
  const key = appLogoObjectKey(context.uid, personaId, version);
  const image = appLogoUrl(c.env, personaId, version);
  await c.env.ASSETS.put(key, parsed.file.bytes, {
    httpMetadata: { contentType: parsed.file.contentType },
    customMetadata: { ownerUid: context.uid, appId: personaId, version },
  });
  let committed = false;
  try {
    const description = await generateDescription(c.env, name, input);
    const now = Math.floor(Date.now() / 1_000);
    const payload: JsonObject = {
      id: personaId,
      name,
      username: personaUsername,
      description,
      image,
      uid: context.uid,
      author: profile.name || name,
      email: profile.email,
      approved: false,
      status: "under-review",
      category: "personality-emulation",
      capabilities: ["persona"],
      connected_accounts: connectedAccounts,
      private: input.private === undefined ? false : input.private,
      persona_prompt:
        stringValue(
          input.persona_prompt,
          "Persona prompt",
          MAX_PROMPT_LENGTH,
        ) || personaPrompt(name, input),
      created_at: new Date().toISOString(),
    };
    const encoded = JSON.stringify(payload);
    if (new TextEncoder().encode(encoded).byteLength > MAX_PERSONA_DATA_BYTES)
      throw new PersonaMutationError(413, "Persona data is too large");
    await c.env.APP_DB.prepare(
      "INSERT INTO cf_app_catalog (id, approved, status, disabled, is_popular, installs, rating_avg, rating_count, data_json, updated_at, owner_uid) VALUES (?, 0, 'under-review', 0, 0, 0, NULL, 0, ?, ?, ?)",
    )
      .bind(personaId, encoded, now, context.uid)
      .run();
    committed = true;
    return c.json({
      status: "ok",
      app_id: personaId,
      username: personaUsername,
    });
  } finally {
    if (!committed) await c.env.ASSETS.delete(key).catch(() => undefined);
  }
}

export function registerPersonaMutationRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
) {
  app.post("/v1/personas", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await createPersona(c, context);
    } catch (error) {
      return errorResponse(c, error);
    }
  });
}
