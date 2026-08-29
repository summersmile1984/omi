import {
  FormDataParseError,
  MaxFilesExceededError,
  MaxFileSizeExceededError,
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
import {
  appLogoObjectKey,
  appLogoObjectKeyFromPayload,
  appLogoUrl,
} from "./app-logo";
import {
  provisionPaidApp,
  setPaidAppLinkActive,
} from "./app-payment-provisioning";
import {
  deactivateAppPaymentLink,
  stripeAppPaymentLink,
  type AppPaymentLinkRow,
} from "./app-payment-links";
import type { JobsEnv } from "./env";
import { StripeConfigurationError, StripeResponseError } from "./stripe-client";

const APP_ID_MAX_LENGTH = 256;
const APP_DATA_MAX_BYTES = 500_000;
const APP_IMAGE_MAX_BYTES = 10 * 1_024 * 1_024;
const APP_REQUEST_MAX_BYTES = APP_IMAGE_MAX_BYTES + APP_DATA_MAX_BYTES + 64_000;
const MAX_NAME_LENGTH = 160;
const MAX_DESCRIPTION_LENGTH = 20_000;
const MAX_TEXT_LENGTH = 100_000;
const MAX_LIST_LENGTH = 100;
const MAX_URL_LENGTH = 2_048;
const MAX_PRICE_CENTS = 100_000_000;
const MAX_MANIFEST_BYTES = 256_000;
const MAX_MANIFEST_TOOLS = 50;
const ACTION_TYPES = new Set([
  "create_conversation",
  "create_facts",
  "read_memories",
  "read_conversations",
  "read_tasks",
]);

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;
type JsonObject = Record<string, unknown>;
type CatalogRow = {
  id: string;
  owner_uid: string | null;
  approved: number;
  status: string;
  disabled: number;
  is_popular: number;
  installs: number;
  rating_avg: number | null;
  rating_count: number;
  data_json: string;
};
type AuthProfile = { uid: string; name: string | null; email: string | null };
type StagedLogo = {
  key: string;
  url: string;
  version: string;
  contentType: string;
};
type LogoCleanupReason = "uncommitted-upload" | "superseded" | "deleted";

class AppMutationError extends Error {
  constructor(
    readonly status: 400 | 403 | 404 | 409 | 413 | 422 | 502 | 503,
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

function validAppId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= APP_ID_MAX_LENGTH &&
    !value.includes("/") &&
    !value.includes("\\")
  );
}

function ulid(now = Date.now()) {
  const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  let timestamp = now;
  let result = "";
  for (let index = 0; index < 10; index += 1) {
    result = alphabet[timestamp % 32] + result;
    timestamp = Math.floor(timestamp / 32);
  }
  const random = crypto.getRandomValues(new Uint8Array(16));
  let accumulator = 0;
  let bits = 0;
  for (const byte of random) {
    accumulator = (accumulator << 8) | byte;
    bits += 8;
    while (bits >= 5 && result.length < 26) {
      bits -= 5;
      result += alphabet[(accumulator >>> bits) & 31];
    }
  }
  while (result.length < 26) result += alphabet[0];
  return result;
}

function stringValue(
  value: unknown,
  name: string,
  maximum: number,
  options: { required?: boolean; trim?: boolean } = {},
) {
  if (value === undefined || value === null) {
    if (options.required)
      throw new AppMutationError(422, `${name} is required`);
    return null;
  }
  if (typeof value !== "string") {
    throw new AppMutationError(422, `${name} is invalid`);
  }
  const result = options.trim === false ? value : value.trim();
  if ((options.required && !result) || result.length > maximum) {
    throw new AppMutationError(422, `${name} is invalid`);
  }
  return result;
}

function booleanValue(value: unknown, name: string, fallback: boolean) {
  if (value === undefined || value === null) return fallback;
  if (typeof value !== "boolean") {
    throw new AppMutationError(422, `${name} is invalid`);
  }
  return value;
}

function stringList(value: unknown, name: string, maximum = MAX_LIST_LENGTH) {
  if (!Array.isArray(value) || value.length > maximum) {
    throw new AppMutationError(422, `${name} is invalid`);
  }
  const result = value.map((item) => {
    if (typeof item !== "string" || !item.trim() || item.length > 256) {
      throw new AppMutationError(422, `${name} is invalid`);
    }
    return item.trim();
  });
  return [...new Set(result)];
}

function httpsUrl(value: unknown, name: string, required = false) {
  const raw = stringValue(value, name, MAX_URL_LENGTH, { required });
  if (raw === null || raw === "") return raw;
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new AppMutationError(422, `${name} is invalid`);
  }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password) {
    throw new AppMutationError(422, `${name} is invalid`);
  }
  return parsed.toString();
}

export function publicHttpsUrl(value: string) {
  const parsed = new URL(value);
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host.endsWith(".local") ||
    host === "::1" ||
    host.startsWith("fc") ||
    host.startsWith("fd") ||
    host.startsWith("fe80:")
  ) {
    return false;
  }
  const octets = host.split(".").map(Number);
  if (
    octets.length === 4 &&
    octets.every(
      (octet) => Number.isInteger(octet) && octet >= 0 && octet <= 255,
    )
  ) {
    return !(
      octets[0] === 0 ||
      octets[0] === 10 ||
      octets[0] === 127 ||
      (octets[0] === 169 && octets[1] === 254) ||
      (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
      (octets[0] === 192 && octets[1] === 168)
    );
  }
  return true;
}

async function boundedJsonResponse(response: Response, maximum: number) {
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && (declared < 0 || declared > maximum)) {
    response.body?.cancel();
    throw new Error("response body is too large");
  }
  if (!response.body) throw new Error("response body is missing");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const item = await reader.read();
      if (item.done) break;
      size += item.value.byteLength;
      if (size > maximum) throw new Error("response body is too large");
      chunks.push(item.value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return JSON.parse(
    new TextDecoder("utf-8", { fatal: true }).decode(bytes),
  ) as unknown;
}

function normalizedManifestTool(value: unknown, appHomeUrl: string | null) {
  const tool = objectValue(value);
  if (!tool) return null;
  const name = stringValue(tool.name, "Tool name", 160);
  const description = stringValue(tool.description, "Tool description", 4_000, {
    trim: false,
  });
  const rawEndpoint = stringValue(
    tool.endpoint,
    "Tool endpoint",
    MAX_URL_LENGTH,
  );
  if (!name || !description || !rawEndpoint) return null;
  let endpoint = rawEndpoint;
  if (endpoint.startsWith("/") && !endpoint.startsWith("//") && appHomeUrl) {
    endpoint = `${appHomeUrl.replace(/\/$/, "")}${endpoint}`;
  } else if (!endpoint.startsWith("/")) {
    try {
      const parsed = new URL(endpoint);
      if (parsed.protocol !== "https:" || parsed.username || parsed.password)
        return null;
      endpoint = parsed.toString();
    } catch {
      return null;
    }
  }
  const rawMethod =
    typeof tool.method === "string" ? tool.method.toUpperCase() : "POST";
  const method = ["GET", "POST", "PUT", "PATCH", "DELETE"].includes(rawMethod)
    ? rawMethod
    : "POST";
  const result: JsonObject = {
    name,
    description,
    endpoint,
    method,
    auth_required: tool.auth_required !== false,
  };
  const status = stringValue(tool.status_message, "Tool status message", 500);
  if (status) result.status_message = status;
  const parameters = objectValue(tool.parameters);
  if (parameters) {
    const properties = objectValue(parameters.properties);
    if (properties) {
      const required = Array.isArray(parameters.required)
        ? parameters.required.filter(
            (item): item is string =>
              typeof item === "string" && item.length <= 160,
          )
        : [];
      result.parameters = { properties, required };
    }
  }
  return result;
}

async function applyChatToolsManifest(
  payload: JsonObject,
  requestId: string,
  options: { strict?: boolean } = {},
) {
  const external = objectValue(payload.external_integration);
  if (!external) return;
  const manifestUrl =
    typeof external?.chat_tools_manifest_url === "string"
      ? external.chat_tools_manifest_url
      : null;
  if (!manifestUrl) return 0;
  try {
    if (!publicHttpsUrl(manifestUrl))
      throw new Error("manifest URL is private");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10_000);
    let response: Response;
    try {
      response = await fetch(manifestUrl, {
        headers: { accept: "application/json" },
        redirect: "manual",
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeout);
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new Error("manifest request failed");
    }
    const manifest = objectValue(
      await boundedJsonResponse(response, MAX_MANIFEST_BYTES),
    );
    if (
      !manifest ||
      !Array.isArray(manifest.tools) ||
      manifest.tools.length > MAX_MANIFEST_TOOLS
    ) {
      throw new Error("manifest response is invalid");
    }
    const appHomeUrl =
      typeof external.app_home_url === "string" ? external.app_home_url : null;
    const tools = manifest.tools
      .map((tool) => {
        try {
          return normalizedManifestTool(tool, appHomeUrl);
        } catch {
          return null;
        }
      })
      .filter((tool): tool is JsonObject => tool !== null);
    if (tools.length) payload.chat_tools = tools;
    const chatMessages = objectValue(manifest.chat_messages);
    if (chatMessages?.enabled === true) {
      external.chat_messages_enabled = true;
      external.chat_messages_target =
        chatMessages.target === "main" ? "main" : "app";
      external.chat_messages_notify = chatMessages.notify !== false;
    } else {
      external.chat_messages_enabled = false;
      external.chat_messages_target = "app";
      external.chat_messages_notify = false;
    }
    return tools.length;
  } catch {
    recordFallback({
      component: "other",
      from: "none",
      to: "none",
      reason: "dependency_unavailable",
      outcome: "degraded",
      requestId,
    });
    if (options.strict) {
      throw new AppMutationError(
        502,
        "Failed to fetch manifest from external URL",
      );
    }
    return 0;
  }
}

function normalizedWebhook(value: unknown) {
  const raw = stringValue(value, "Webhook URL", MAX_URL_LENGTH, {
    required: true,
  });
  if (!raw) throw new AppMutationError(422, "Webhook URL is required");
  return httpsUrl(
    raw.includes("://") ? raw : `https://${raw}`,
    "Webhook URL",
    true,
  );
}

function externalIntegration(value: unknown): JsonObject | null {
  if (value === undefined || value === null) return null;
  const input = objectValue(value);
  if (!input)
    throw new AppMutationError(422, "External integration is invalid");
  for (const forbidden of ["mcp_oauth_tokens", "mcp_server_url"]) {
    if (input[forbidden] !== undefined) {
      throw new AppMutationError(
        422,
        "MCP credentials require the dedicated MCP route",
      );
    }
  }
  const triggersOn = stringValue(input.triggers_on, "Trigger", 80);
  const rawActions = input.actions === undefined ? [] : input.actions;
  if (!Array.isArray(rawActions) || rawActions.length > 20) {
    throw new AppMutationError(422, "Actions are invalid");
  }
  const actions = rawActions.map((raw) => {
    const action = objectValue(raw);
    if (
      !action ||
      typeof action.action !== "string" ||
      !ACTION_TYPES.has(action.action)
    ) {
      throw new AppMutationError(422, "Action field is invalid");
    }
    return { action: action.action };
  });
  if (!triggersOn && actions.length === 0) {
    throw new AppMutationError(422, "Triggers on or actions is required");
  }
  const authStepsRaw = input.auth_steps === undefined ? [] : input.auth_steps;
  if (!Array.isArray(authStepsRaw) || authStepsRaw.length > 20) {
    throw new AppMutationError(422, "Auth steps are invalid");
  }
  const authSteps = authStepsRaw.map((raw) => {
    const step = objectValue(raw);
    if (!step) throw new AppMutationError(422, "Auth steps are invalid");
    return {
      name: stringValue(step.name, "Auth step name", 160, { required: true })!,
      url: httpsUrl(step.url, "Auth step URL", true)!,
    };
  });
  const result: JsonObject = {
    triggers_on: triggersOn,
    actions,
    auth_steps: authSteps,
    is_instructions_url: booleanValue(
      input.is_instructions_url,
      "Instructions URL flag",
      true,
    ),
    chat_messages_enabled: booleanValue(
      input.chat_messages_enabled,
      "Chat messages enabled",
      false,
    ),
    chat_messages_target:
      input.chat_messages_target === "main" ? "main" : "app",
    chat_messages_notify: booleanValue(
      input.chat_messages_notify,
      "Chat messages notify",
      false,
    ),
  };
  if (triggersOn) result.webhook_url = normalizedWebhook(input.webhook_url);
  for (const [key, label] of [
    ["setup_completed_url", "Setup completed URL"],
    ["app_home_url", "App home URL"],
    ["chat_tools_manifest_url", "Chat tools manifest URL"],
  ] as const) {
    const parsed = httpsUrl(input[key], label);
    if (parsed) result[key] = parsed;
  }
  const instructions = stringValue(
    input.setup_instructions_file_path,
    "Setup instructions",
    MAX_URL_LENGTH,
  );
  if (instructions) result.setup_instructions_file_path = instructions;
  if (!result.app_home_url && authSteps[0])
    result.app_home_url = authSteps[0].url;
  return result;
}

function priceCents(value: unknown, paid: boolean) {
  if (!paid) return 0;
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new AppMutationError(422, "App price must be a positive number");
  }
  const cents = Math.round(value * 100);
  if (!Number.isSafeInteger(cents) || cents < 1 || cents > MAX_PRICE_CENTS) {
    throw new AppMutationError(422, "App price is invalid");
  }
  return cents;
}

function payloadFromInput(
  input: JsonObject,
  base: JsonObject | null,
  identity: {
    appId: string;
    uid: string;
    image: string;
    profile?: AuthProfile;
  },
) {
  const creating = base === null;
  const result: JsonObject = base ? { ...base } : {};
  if (input.id !== undefined && input.id !== identity.appId) {
    throw new AppMutationError(422, "App id does not match");
  }
  if (input.uid !== undefined && input.uid !== identity.uid) {
    throw new AppMutationError(403, "App owner does not match");
  }
  const name = stringValue(
    input.name ?? result.name,
    "App name",
    MAX_NAME_LENGTH,
    { required: true },
  )!;
  const description = stringValue(
    input.description ?? result.description,
    "Description",
    MAX_DESCRIPTION_LENGTH,
    { required: true, trim: false },
  )!;
  const category = stringValue(
    input.category ?? result.category,
    "Category",
    160,
    { required: true },
  )!;
  const capabilities = stringList(
    input.capabilities ?? result.capabilities,
    "Capabilities",
    20,
  );
  if (!capabilities.length || capabilities.includes("persona")) {
    throw new AppMutationError(422, "Capabilities are invalid");
  }
  const paid = booleanValue(
    input.is_paid,
    "Paid app flag",
    result.is_paid === true,
  );
  const price = paid
    ? typeof input.price === "number"
      ? input.price
      : result.price
    : 0;
  const cents = priceCents(price, paid);
  const paymentPlan = paid
    ? stringValue(
        input.payment_plan ?? result.payment_plan,
        "Payment plan",
        80,
        { required: true },
      )
    : null;
  if (paid && paymentPlan !== "monthly_recurring") {
    throw new AppMutationError(422, "Payment plan is invalid");
  }
  const ext =
    input.external_integration !== undefined
      ? externalIntegration(input.external_integration)
      : (objectValue(result.external_integration) ?? null);
  if (capabilities.includes("external_integration") && !ext) {
    throw new AppMutationError(422, "External integration is required");
  }
  if (!capabilities.includes("external_integration"))
    result.external_integration = null;
  else result.external_integration = ext;
  for (const [key, maximum] of [
    ["memory_prompt", MAX_TEXT_LENGTH],
    ["chat_prompt", MAX_TEXT_LENGTH],
    ["source_code_url", MAX_URL_LENGTH],
  ] as const) {
    if (input[key] !== undefined) {
      result[key] =
        key === "source_code_url"
          ? httpsUrl(input[key], "Source code URL")
          : stringValue(input[key], key, maximum, { trim: false });
    }
  }
  if (input.proactive_notification !== undefined) {
    const proactive = objectValue(input.proactive_notification);
    if (!proactive)
      throw new AppMutationError(422, "Notification scopes are invalid");
    result.proactive_notification = {
      scopes: stringList(proactive.scopes, "Notification scopes"),
    };
  } else if (input.proactive_notification_scopes !== undefined) {
    result.proactive_notification = {
      scopes: stringList(
        input.proactive_notification_scopes,
        "Notification scopes",
      ),
    };
  }
  for (const key of ["connected_accounts", "thumbnails"] as const) {
    if (input[key] !== undefined) result[key] = stringList(input[key], key);
  }
  const author = stringValue(
    input.author ?? result.author ?? identity.profile?.name,
    "Author",
    160,
  );
  const email = stringValue(
    input.email ?? result.email ?? identity.profile?.email,
    "Email",
    320,
  );
  result.id = identity.appId;
  result.uid = identity.uid;
  result.name = name;
  result.description = description;
  result.category = category;
  result.author = author || email?.split("@")[0] || "Anonymous";
  result.email = email;
  result.image = identity.image;
  result.capabilities = capabilities;
  result.private = booleanValue(
    input.private,
    "Private flag",
    result.private === true,
  );
  result.is_paid = paid;
  result.price = paid ? cents / 100 : 0;
  result.payment_plan = paymentPlan;
  if (creating) {
    result.approved = false;
    result.status = "under-review";
    result.created_at = new Date().toISOString();
  }
  result.updated_at = new Date().toISOString();
  for (const forbidden of [
    "payment_product_id",
    "payment_price_id",
    "payment_link_id",
    "payment_link",
    "reviews",
    "user_review",
    "money_made",
    "usage_count",
    "chat_tools",
    "persona_prompt",
  ]) {
    if (input[forbidden] !== undefined) {
      throw new AppMutationError(
        422,
        `${forbidden} requires a dedicated route`,
      );
    }
  }
  return { payload: result, paid, cents };
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
    throw new AppMutationError(422, "App image is invalid");
  }
  return detected;
}

async function parseMultipart(
  request: Request,
  env: JobsEnv,
  ownerUid: string,
  appId: string,
) {
  const staged = { value: null as StagedLogo | null };
  try {
    const form = await parseFormData(
      request,
      {
        maxFiles: 1,
        maxFileSize: APP_IMAGE_MAX_BYTES,
        maxParts: 4,
        maxTotalSize: APP_REQUEST_MAX_BYTES,
      },
      async (file: FileUpload) => {
        if (file.fieldName !== "file" || staged.value) {
          throw new AppMutationError(400, "App image field is invalid");
        }
        const bytes = new Uint8Array(await file.arrayBuffer());
        if (!bytes.length)
          throw new AppMutationError(422, "App image is empty");
        const contentType = imageType(bytes, file.type);
        const version = crypto.randomUUID();
        const key = appLogoObjectKey(ownerUid, appId, version);
        const url = appLogoUrl(env, appId, version);
        await env.ASSETS.put(key, bytes, {
          httpMetadata: { contentType },
          customMetadata: { ownerUid, appId, version },
        });
        staged.value = { key, url, version, contentType };
        return key;
      },
    );
    const raw = form.get("app_data");
    if (
      typeof raw !== "string" ||
      !raw ||
      new TextEncoder().encode(raw).byteLength > APP_DATA_MAX_BYTES
    ) {
      throw new AppMutationError(400, "app_data is invalid");
    }
    const parsed = objectValue(JSON.parse(raw));
    if (!parsed) throw new AppMutationError(400, "app_data is invalid");
    return { input: parsed, staged: staged.value };
  } catch (error) {
    if (staged.value)
      await cleanupLogo(env, staged.value.key, ownerUid, "uncommitted-upload");
    if (
      error instanceof MaxFilesExceededError ||
      error instanceof MaxFileSizeExceededError ||
      error instanceof MaxTotalSizeExceededError
    ) {
      throw new AppMutationError(413, "App upload is too large");
    }
    if (
      error instanceof FormDataParseError ||
      error instanceof MaxPartsExceededError ||
      error instanceof SyntaxError
    ) {
      throw new AppMutationError(400, "App upload is invalid");
    }
    throw error;
  }
}

async function cleanupLogo(
  env: JobsEnv,
  key: string | null | undefined,
  uid: string,
  reason: LogoCleanupReason,
) {
  if (!key) return;
  try {
    await env.ASSETS.delete(key);
  } catch {
    const now = Math.floor(Date.now() / 1_000);
    try {
      await env.APP_DB.prepare(
        `INSERT INTO cf_asset_cleanup_tasks
           (storage_key, uid, logical_key, reason, not_before,
            attempts, last_error, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, 0, 'r2 delete unavailable', ?, ?)
         ON CONFLICT(storage_key) DO UPDATE SET
           uid = excluded.uid,
           logical_key = excluded.logical_key,
           reason = excluded.reason,
           not_before = MIN(cf_asset_cleanup_tasks.not_before, excluded.not_before),
           last_error = excluded.last_error,
           updated_at = excluded.updated_at`,
      )
        .bind(key, uid, key, reason, now, now, now)
        .run();
    } catch {
      // Account deletion's exhaustive uid-scoped R2 sweep remains the final
      // fallback when its fence intentionally rejects new cleanup tasks.
    }
  }
}

async function authProfile(
  env: JobsEnv,
  context: SignedAuthContext,
): Promise<AuthProfile> {
  const path = "/internal/profile";
  const signed = await createSignedAuthContext(
    {
      uid: context.uid,
      authority: "internal",
      requestId: context.requestId,
    },
    "auth",
    "GET",
    path,
    env.INTERNAL_ASSERTION_SECRET,
  );
  if (!signed) throw new AppMutationError(503, "App profile is unavailable");
  const response = await env.AUTH.fetch(
    new Request(`https://auth.internal${path}`, {
      headers: {
        [AUTH_CONTEXT_HEADER]: signed.encoded,
        [AUTH_SIGNATURE_HEADER]: signed.signature,
        "x-request-id": context.requestId,
      },
    }),
  );
  if (!response.ok) {
    await response.arrayBuffer();
    throw new AppMutationError(503, "App profile is unavailable");
  }
  const profile = objectValue(await response.json());
  if (!profile || profile.uid !== context.uid) {
    throw new AppMutationError(503, "App profile is unavailable");
  }
  return {
    uid: context.uid,
    name: typeof profile.name === "string" ? profile.name : null,
    email: typeof profile.email === "string" ? profile.email : null,
  };
}

function catalogPayload(raw: string, appId: string) {
  if (new TextEncoder().encode(raw).byteLength > APP_DATA_MAX_BYTES)
    throw new Error("app catalog payload is invalid");
  const payload = objectValue(JSON.parse(raw));
  if (!payload || payload.id !== appId)
    throw new Error("app catalog payload is invalid");
  return payload;
}

function serializeCatalogPayload(payload: JsonObject) {
  const raw = JSON.stringify(payload);
  if (new TextEncoder().encode(raw).byteLength > APP_DATA_MAX_BYTES) {
    throw new AppMutationError(413, "App data is too large");
  }
  return raw;
}

function paidPayload(payload: JsonObject, mapping: AppPaymentLinkRow | null) {
  const result = { ...payload };
  for (const key of [
    "payment_product_id",
    "payment_price_id",
    "payment_link_id",
    "payment_link",
  ]) {
    delete result[key];
  }
  if (mapping) {
    result.payment_link = mapping.payment_link_url;
  }
  return result;
}

function paymentInsert(env: JobsEnv, row: AppPaymentLinkRow, now: number) {
  return env.APP_DB.prepare(
    `INSERT INTO cf_app_payment_links
       (app_id, owner_uid, stripe_account_id, stripe_product_id,
        stripe_price_id, stripe_payment_link_id, payment_link_url,
        unit_amount, currency, interval, active, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'usd', 'month', 1, ?, ?)
     ON CONFLICT(app_id) DO UPDATE SET
       owner_uid = excluded.owner_uid,
       stripe_account_id = excluded.stripe_account_id,
       stripe_product_id = excluded.stripe_product_id,
       stripe_price_id = excluded.stripe_price_id,
       stripe_payment_link_id = excluded.stripe_payment_link_id,
       payment_link_url = excluded.payment_link_url,
       unit_amount = excluded.unit_amount,
       currency = excluded.currency,
       interval = excluded.interval,
       active = 1,
       updated_at = excluded.updated_at`,
  ).bind(
    row.app_id,
    row.owner_uid,
    row.stripe_account_id,
    row.stripe_product_id,
    row.stripe_price_id,
    row.stripe_payment_link_id,
    row.payment_link_url,
    row.unit_amount,
    now,
    now,
  );
}

async function provisionIfNeeded(
  env: JobsEnv,
  input: {
    appId: string;
    uid: string;
    payload: JsonObject;
    cents: number;
    existing: AppPaymentLinkRow | null;
    requestId: string;
  },
) {
  if (
    input.existing &&
    input.existing.owner_uid === input.uid &&
    input.existing.unit_amount === input.cents &&
    Number(input.existing.active) === 1
  ) {
    // Treat the existing link as both the committed and rollback authority. If
    // the following D1 update fails, compensation must not deactivate the
    // already-published link merely because no replacement was needed.
    return { mapping: input.existing, replaced: input.existing };
  }
  if (input.existing && input.existing.owner_uid !== input.uid) {
    throw new Error("paid app owner does not match");
  }
  const suffix = `${input.cents}-${input.requestId.slice(-48)}`;
  const mapping = await provisionPaidApp(env, {
    appId: input.appId,
    ownerUid: input.uid,
    name: String(input.payload.name),
    description: String(input.payload.description),
    image: String(input.payload.image),
    unitAmount: input.cents,
    productId: input.existing?.stripe_product_id,
    idempotencySuffix: suffix,
  });
  if (input.existing) {
    try {
      await deactivateAppPaymentLink(
        env,
        input.existing,
        `app-update-${input.requestId}`,
      );
    } catch (error) {
      try {
        await setPaidAppLinkActive(
          env,
          mapping,
          false,
          `abort-${input.requestId.slice(-48)}`,
        );
      } catch {
        // The replacement URL has never been published. Keep the original
        // provider failure as the authoritative error.
      }
      throw error;
    }
  }
  return { mapping, replaced: input.existing };
}

async function compensatePayment(
  env: JobsEnv,
  mapping: AppPaymentLinkRow | null,
  replaced: AppPaymentLinkRow | null,
  requestId: string,
) {
  if (mapping && mapping === replaced) return;
  if (mapping && mapping !== replaced) {
    try {
      await setPaidAppLinkActive(
        env,
        mapping,
        false,
        `rollback-${requestId.slice(-48)}`,
      );
    } catch {
      // The new URL was never published. Provider reconciliation can retire it.
    }
  }
  if (replaced) {
    try {
      await setPaidAppLinkActive(
        env,
        replaced,
        true,
        `restore-${requestId.slice(-48)}`,
      );
    } catch {
      // Fail closed: a temporarily inactive legacy link cannot collect money.
    }
  }
}

async function createApp(c: JobsContext, context: SignedAuthContext) {
  if (!validAccountDeletionUid(context.uid)) {
    throw new AppMutationError(403, "App owner is invalid");
  }
  const appId = ulid();
  const parsed = await parseMultipart(c.req.raw, c.env, context.uid, appId);
  if (!parsed.staged) throw new AppMutationError(422, "App image is required");
  let committed = false;
  let mapping: AppPaymentLinkRow | null = null;
  try {
    const profile = await authProfile(c.env, context);
    const normalized = payloadFromInput(parsed.input, null, {
      appId,
      uid: context.uid,
      image: parsed.staged.url,
      profile,
    });
    if (
      objectValue(parsed.input.external_integration)?.chat_tools_manifest_url
    ) {
      await applyChatToolsManifest(normalized.payload, context.requestId);
    }
    // Validate the fully hydrated catalog document before creating provider
    // resources. A bounded manifest can still push a large app over D1's
    // application-level payload limit.
    serializeCatalogPayload(normalized.payload);
    let replaced: AppPaymentLinkRow | null = null;
    if (normalized.paid) {
      const payment = await provisionIfNeeded(c.env, {
        appId,
        uid: context.uid,
        payload: normalized.payload,
        cents: normalized.cents,
        existing: null,
        requestId: context.requestId,
      });
      mapping = payment.mapping;
      replaced = payment.replaced;
    }
    const payload = paidPayload(normalized.payload, mapping);
    const now = Math.floor(Date.now() / 1_000);
    try {
      const statements = [
        c.env.APP_DB.prepare(
          `INSERT INTO cf_app_catalog
             (id, approved, status, disabled, is_popular, installs,
              rating_avg, rating_count, data_json, updated_at, owner_uid)
           VALUES (?, 0, 'under-review', 0, 0, 0, NULL, 0, ?, ?, ?)`,
        ).bind(appId, serializeCatalogPayload(payload), now, context.uid),
      ];
      if (mapping) statements.push(paymentInsert(c.env, mapping, now));
      await c.env.APP_DB.batch(statements);
      committed = true;
    } catch (error) {
      await compensatePayment(c.env, mapping, replaced, context.requestId);
      throw error;
    }
    return c.json({ status: "ok", app_id: appId });
  } finally {
    if (!committed)
      await cleanupLogo(
        c.env,
        parsed.staged.key,
        context.uid,
        "uncommitted-upload",
      );
  }
}

async function updateApp(
  c: JobsContext,
  context: SignedAuthContext,
  appId: string,
) {
  if (!validAppId(appId) || !validAccountDeletionUid(context.uid)) {
    throw new AppMutationError(404, "App not found");
  }
  const current = await c.env.APP_DB.prepare(
    `SELECT id, owner_uid, approved, status, disabled, is_popular, installs,
            rating_avg, rating_count, data_json
     FROM cf_app_catalog WHERE id = ? LIMIT 1`,
  )
    .bind(appId)
    .first<CatalogRow>();
  if (!current) throw new AppMutationError(404, "App not found");
  if (current.owner_uid !== context.uid) {
    throw new AppMutationError(
      403,
      "You are not authorized to perform this action",
    );
  }
  const existingPayload = catalogPayload(current.data_json, appId);
  const parsed = await parseMultipart(c.req.raw, c.env, context.uid, appId);
  let committed = false;
  let mapping: AppPaymentLinkRow | null = null;
  let replaced: AppPaymentLinkRow | null = null;
  try {
    const oldLogoKey = appLogoObjectKeyFromPayload(
      current.data_json,
      context.uid,
      appId,
    );
    const image = parsed.staged?.url || String(existingPayload.image || "");
    if (!image) throw new AppMutationError(422, "App image is required");
    const existingPayment = await stripeAppPaymentLink(c.env, appId);
    const normalized = payloadFromInput(parsed.input, existingPayload, {
      appId,
      uid: context.uid,
      image,
    });
    if (
      objectValue(parsed.input.external_integration)?.chat_tools_manifest_url
    ) {
      await applyChatToolsManifest(normalized.payload, context.requestId);
    }
    serializeCatalogPayload(normalized.payload);
    if (normalized.paid) {
      const payment = await provisionIfNeeded(c.env, {
        appId,
        uid: context.uid,
        payload: normalized.payload,
        cents: normalized.cents,
        existing: existingPayment,
        requestId: context.requestId,
      });
      mapping = payment.mapping;
      replaced = payment.replaced;
    } else if (existingPayment && Number(existingPayment.active) === 1) {
      await deactivateAppPaymentLink(
        c.env,
        existingPayment,
        `app-free-${context.requestId}`,
      );
      replaced = existingPayment;
    }
    const payload = paidPayload(normalized.payload, mapping);
    const now = Math.floor(Date.now() / 1_000);
    try {
      const statements = [
        c.env.APP_DB.prepare(
          `UPDATE cf_app_catalog
           SET data_json = ?, updated_at = ?
           WHERE id = ? AND owner_uid = ?`,
        ).bind(serializeCatalogPayload(payload), now, appId, context.uid),
      ];
      if (mapping) statements.push(paymentInsert(c.env, mapping, now));
      else if (existingPayment) {
        statements.push(
          c.env.APP_DB.prepare(
            `UPDATE cf_app_payment_links SET active = 0, updated_at = ?
             WHERE app_id = ? AND owner_uid = ?`,
          ).bind(now, appId, context.uid),
        );
      }
      const results = await c.env.APP_DB.batch(statements);
      if (Number(results[0]?.meta?.changes) < 1) {
        throw new Error("app update lost its catalog authority");
      }
      committed = true;
    } catch (error) {
      await compensatePayment(c.env, mapping, replaced, context.requestId);
      throw error;
    }
    if (parsed.staged && oldLogoKey && oldLogoKey !== parsed.staged.key) {
      await cleanupLogo(c.env, oldLogoKey, context.uid, "superseded");
    }
    return c.json({ status: "ok" });
  } finally {
    if (!committed && parsed.staged)
      await cleanupLogo(
        c.env,
        parsed.staged.key,
        context.uid,
        "uncommitted-upload",
      );
  }
}

function privateQuery(c: JobsContext) {
  const raw = c.req.query("private")?.trim().toLowerCase();
  if (["true", "1", "on", "yes"].includes(raw || "")) return true;
  if (["false", "0", "off", "no"].includes(raw || "")) return false;
  throw new AppMutationError(422, "private is invalid");
}

async function changeAppVisibility(
  c: JobsContext,
  context: SignedAuthContext,
  appId: string,
) {
  if (!validAppId(appId) || !validAccountDeletionUid(context.uid)) {
    throw new AppMutationError(404, "App not found");
  }
  const row = await c.env.APP_DB.prepare(
    "SELECT owner_uid, data_json FROM cf_app_catalog WHERE id = ? LIMIT 1",
  )
    .bind(appId)
    .first<{ owner_uid: string | null; data_json: string }>();
  if (!row) throw new AppMutationError(404, "App not found");
  if (row.owner_uid !== context.uid) {
    throw new AppMutationError(
      403,
      "You are not authorized to perform this action",
    );
  }
  const payload = catalogPayload(row.data_json, appId);
  payload.private = privateQuery(c);
  payload.updated_at = new Date().toISOString();
  const now = Math.floor(Date.now() / 1_000);
  const result = await c.env.APP_DB.prepare(
    `UPDATE cf_app_catalog
     SET data_json = ?, updated_at = ?
     WHERE id = ? AND owner_uid = ? AND data_json = ?`,
  )
    .bind(
      serializeCatalogPayload(payload),
      now,
      appId,
      context.uid,
      row.data_json,
    )
    .run();
  if (Number(result.meta?.changes) !== 1) {
    throw new AppMutationError(409, "App changed during visibility update");
  }
  return c.json({ status: "ok" });
}

async function refreshAppManifest(
  c: JobsContext,
  context: SignedAuthContext,
  appId: string,
) {
  if (!validAppId(appId) || !validAccountDeletionUid(context.uid)) {
    throw new AppMutationError(404, "App not found");
  }
  const row = await c.env.APP_DB.prepare(
    "SELECT owner_uid, data_json FROM cf_app_catalog WHERE id = ? LIMIT 1",
  )
    .bind(appId)
    .first<{ owner_uid: string | null; data_json: string }>();
  if (!row) throw new AppMutationError(404, "App not found");
  if (row.owner_uid !== context.uid) {
    throw new AppMutationError(
      403,
      "You are not authorized to perform this action",
    );
  }
  const payload = catalogPayload(row.data_json, appId);
  const external = objectValue(payload.external_integration);
  if (!external) {
    throw new AppMutationError(400, "App does not have external integration");
  }
  if (
    typeof external.chat_tools_manifest_url !== "string" ||
    !external.chat_tools_manifest_url
  ) {
    throw new AppMutationError(
      400,
      "App does not have a chat tools manifest URL",
    );
  }
  const toolsCount = await applyChatToolsManifest(payload, context.requestId, {
    strict: true,
  });
  payload.updated_at = new Date().toISOString();
  const now = Math.floor(Date.now() / 1_000);
  const result = await c.env.APP_DB.prepare(
    `UPDATE cf_app_catalog
     SET data_json = ?, updated_at = ?
     WHERE id = ? AND owner_uid = ? AND data_json = ?`,
  )
    .bind(
      serializeCatalogPayload(payload),
      now,
      appId,
      context.uid,
      row.data_json,
    )
    .run();
  if (Number(result.meta?.changes) !== 1) {
    throw new AppMutationError(409, "App changed during manifest refresh");
  }
  return c.json({ status: "ok", tools_count: toolsCount });
}

async function publicLogo(c: JobsContext, appId: string, version: string) {
  if (!validAppId(appId)) return c.json({ detail: "App not found" }, 404);
  let expectedKey: string;
  try {
    expectedKey = appLogoObjectKey("placeholder", appId, version).replace(
      "cf-app-logos/placeholder/",
      "",
    );
  } catch {
    return c.json({ detail: "App not found" }, 404);
  }
  try {
    const row = await c.env.APP_DB.prepare(
      "SELECT owner_uid, data_json FROM cf_app_catalog WHERE id = ? LIMIT 1",
    )
      .bind(appId)
      .first<{ owner_uid: string | null; data_json: string }>();
    if (!row || !row.owner_uid) return c.json({ detail: "App not found" }, 404);
    const key = appLogoObjectKeyFromPayload(
      row.data_json,
      row.owner_uid,
      appId,
    );
    if (!key || !key.endsWith(expectedKey)) {
      return c.json({ detail: "App not found" }, 404);
    }
    const object = await c.env.ASSETS.get(key);
    if (!object) return c.json({ detail: "App not found" }, 404);
    const headers = new Headers({
      "cache-control": "public, max-age=31536000, immutable",
      "content-security-policy": "default-src 'none'",
      "x-content-type-options": "nosniff",
    });
    object.writeHttpMetadata(headers);
    headers.set("etag", object.httpEtag);
    return new Response(object.body, { headers });
  } catch {
    return c.json({ error: "app_logo_unavailable" }, 503);
  }
}

function mutationResponse(c: JobsContext, error: unknown) {
  if (error instanceof AppMutationError) {
    return c.json({ detail: error.detail }, error.status);
  }
  if (error instanceof StripeConfigurationError) {
    return c.json({ error: "creator_payments_unavailable" }, 503);
  }
  if (error instanceof StripeResponseError) {
    return c.json(
      { detail: error.userMessage || "Stripe request failed" },
      400,
    );
  }
  return c.json({ error: "app_mutation_unavailable" }, 503);
}

export function registerAppMutationRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
) {
  app.get("/v1/apps/:appId/logo/:version", (c) =>
    publicLogo(c, c.req.param("appId"), c.req.param("version")),
  );
  app.post("/v1/apps", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await createApp(c, context);
    } catch (error) {
      return mutationResponse(c, error);
    }
  });
  app.patch("/v1/apps/:appId", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await updateApp(c, context, c.req.param("appId"));
    } catch (error) {
      return mutationResponse(c, error);
    }
  });
  app.patch("/v1/apps/:appId/change-visibility", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await changeAppVisibility(c, context, c.req.param("appId"));
    } catch (error) {
      return mutationResponse(c, error);
    }
  });
  app.post("/v1/apps/:appId/refresh-manifest", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await refreshAppManifest(c, context, c.req.param("appId"));
    } catch (error) {
      return mutationResponse(c, error);
    }
  });
}
