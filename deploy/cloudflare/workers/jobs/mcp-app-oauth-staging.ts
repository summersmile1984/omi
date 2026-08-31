/**
 * Namespaced external-MCP OAuth staging boundary.
 *
 * This is deliberately separate from Better Auth's MCP OAuth (which
 * authenticates a client to Omi's MCP server) and from the legacy
 * /v1/apps/mcp routes. It only proves the provider authorization-code
 * transaction: metadata/registration -> PKCE redirect -> one-time callback
 * exchange -> encrypted credential connection. Tool discovery and app install
 * remain explicit follow-up gates.
 */

import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobsEnv } from "./env";

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;
type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type McpAppOauthDependencies = Readonly<{
  fetchImpl?: FetchLike;
  now?: () => number;
}>;

/**
 * The explicit v2 seam and the old app-install surface share one provider
 * adapter, but they intentionally keep different callback paths and response
 * envelopes.  Keeping the distinction here prevents an exact-route alias from
 * accidentally advertising the v2 contract to released clients.
 */
export type McpAppOauthSurface = "namespaced" | "legacy";

type JsonObject = Record<string, unknown>;

class McpAppOauthError extends Error {
  constructor(
    readonly status: 400 | 401 | 404 | 409 | 413 | 422 | 502 | 503,
    readonly code: string,
  ) {
    super(code);
    this.name = "McpAppOauthError";
  }
}

const MAX_BODY_BYTES = 24_000;
const MAX_PROVIDER_RESPONSE_BYTES = 256_000;
const MAX_METADATA_BYTES = 100_000;
const MAX_CREDENTIAL_BYTES = 8_192;
const MAX_SCOPES = 64;
const MAX_TOOLS = 256;
const MAX_TOOL_NAME_BYTES = 256;
const MAX_TOOL_DESCRIPTION_BYTES = 8_192;
const MAX_TOOL_SCHEMA_BYTES = 512_000;
const MAX_TOOL_SCHEMA_DEPTH = 16;
const MAX_TOOL_SCHEMA_PROPERTIES = 256;
const MCP_PROTOCOL_VERSION = "2025-03-26";
const SUPPORTED_MCP_PROTOCOL_VERSIONS = new Set([
  MCP_PROTOCOL_VERSION,
  "2024-11-05",
]);
const STATE_TTL_SECONDS = 600;
const MAX_ENDPOINT_LENGTH = 2_048;
const MAX_ENDPOINT_CANDIDATES = 8;
const MAX_TOOLS_PAGES = 16;
const MAX_CURSOR_BYTES = 512;
const OPAQUE_RE = /^[A-Za-z0-9._~-]{16,512}$/;
const CLIENT_ID_RE = /^[\x21-\x7e]{1,2048}$/;

// Workers' DOM typings model Uint8Array's backing buffer as ArrayBufferLike,
// while the Web Crypto declarations require BufferSource. These values are
// freshly allocated and safe to pass to Web Crypto.
function cryptoBytes(value: Uint8Array): BufferSource {
  return value as unknown as BufferSource;
}

function nowSeconds(dependencies: McpAppOauthDependencies): number {
  const now = dependencies.now?.() ?? Math.floor(Date.now() / 1_000);
  return Number.isSafeInteger(now) && now > 0 ? now : 0;
}

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function objectValue(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/g, "");
}

function decodeBase64Url(value: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("invalid envelope");
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function sha256(value: string): Promise<Uint8Array> {
  return new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
  );
}

async function sha256Hex(value: string): Promise<string> {
  return Array.from(await sha256(value), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function randomOpaque(bytes = 32): string {
  return base64Url(crypto.getRandomValues(new Uint8Array(bytes)));
}

function encryptionSecret(env: JobsEnv): string {
  const value = env.MCP_APP_TOKEN_ENCRYPTION_SECRET?.trim();
  if (!value || utf8Bytes(value) < 32)
    throw new McpAppOauthError(503, "mcp_app_oauth_unavailable");
  return value;
}

async function key(env: JobsEnv): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    cryptoBytes(await sha256(encryptionSecret(env))),
    { name: "AES-GCM" },
    false,
    ["encrypt", "decrypt"],
  );
}

function aad(
  field: "verifier" | "client_credentials" | "connection",
  ownerUid: string,
  appId: string,
  transactionId?: string,
): Uint8Array {
  return new TextEncoder().encode(
    `omi:mcp-app-oauth:v1\0${field}\0${ownerUid}\0${appId}\0${transactionId || ""}`,
  );
}

async function encrypt(
  env: JobsEnv,
  field: "verifier" | "client_credentials" | "connection",
  ownerUid: string,
  appId: string,
  value: JsonObject,
  transactionId?: string,
): Promise<string> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    {
      name: "AES-GCM",
      iv: cryptoBytes(iv),
      additionalData: cryptoBytes(aad(field, ownerUid, appId, transactionId)),
    },
    await key(env),
    cryptoBytes(new TextEncoder().encode(JSON.stringify(value))),
  );
  return `v1.${base64Url(iv)}.${base64Url(new Uint8Array(ciphertext))}`;
}

async function decrypt(
  env: JobsEnv,
  field: "verifier" | "client_credentials" | "connection",
  ownerUid: string,
  appId: string,
  envelope: string,
  transactionId?: string,
): Promise<JsonObject> {
  const parts = envelope.split(".");
  if (parts.length !== 3 || parts[0] !== "v1" || utf8Bytes(envelope) > 400_000)
    throw new Error("invalid envelope");
  const plaintext = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: cryptoBytes(decodeBase64Url(parts[1])),
      additionalData: cryptoBytes(aad(field, ownerUid, appId, transactionId)),
    },
    await key(env),
    cryptoBytes(decodeBase64Url(parts[2])),
  );
  const value = objectValue(JSON.parse(new TextDecoder().decode(plaintext)));
  if (!value) throw new Error("invalid envelope payload");
  return value;
}

function publicHttps(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    !value ||
    utf8Bytes(value) > MAX_ENDPOINT_LENGTH
  )
    return false;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.hash ||
      host === "localhost" ||
      host.endsWith(".local") ||
      isPrivateIpLiteral(host)
    )
      return false;
    return true;
  } catch {
    return false;
  }
}

function normalizeEndpointCandidates(
  primary: unknown,
  extras: unknown,
): string[] {
  if (!publicHttps(primary))
    throw new McpAppOauthError(422, "invalid_provider_metadata");
  if (extras !== undefined && !Array.isArray(extras))
    throw new McpAppOauthError(422, "invalid_provider_metadata");
  const values = [primary, ...(Array.isArray(extras) ? extras : [])];
  if (values.length > MAX_ENDPOINT_CANDIDATES)
    throw new McpAppOauthError(422, "invalid_provider_metadata");
  const candidates: string[] = [];
  for (const value of values) {
    if (!publicHttps(value))
      throw new McpAppOauthError(422, "invalid_provider_metadata");
    if (!candidates.includes(value)) candidates.push(value);
  }
  return candidates;
}

function metadataEndpointCandidates(
  primary: string,
  resolved: string | null | undefined,
  metadataJson: string | null | undefined,
): string[] {
  const values: unknown[] = [];
  if (resolved) values.push(resolved);
  values.push(primary);
  try {
    const metadata = metadataJson
      ? objectValue(JSON.parse(metadataJson))
      : null;
    if (Array.isArray(metadata?.endpoint_candidates))
      values.push(...metadata.endpoint_candidates);
  } catch {
    // Older rows and a corrupt optional projection still retain server_url.
  }
  const candidates: string[] = [];
  for (const value of values) {
    if (publicHttps(value) && !candidates.includes(value)) {
      candidates.push(value);
      if (candidates.length === MAX_ENDPOINT_CANDIDATES) break;
    }
  }
  return candidates;
}

function isPrivateIpLiteral(host: string): boolean {
  const ipv4 = host.split(".");
  if (ipv4.length === 4 && ipv4.every((part) => /^\d{1,3}$/.test(part))) {
    const octets = ipv4.map(Number);
    if (octets.some((value) => value > 255)) return true;
    const [a, b] = octets;
    return (
      a === 0 ||
      a === 10 ||
      (a === 100 && b >= 64 && b <= 127) ||
      a === 127 ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 0) ||
      (a === 192 && b === 168) ||
      (a === 198 && b >= 18 && b <= 19) ||
      a >= 224
    );
  }
  if (!host.includes(":")) return false;
  const normalized = host.toLowerCase();
  const mapped = normalized.match(/^(?:::ffff:|::)(\d+\.\d+\.\d+\.\d+)$/);
  if (mapped && isPrivateIpLiteral(mapped[1])) return true;
  const halves = normalized.split("::");
  if (halves.length > 2) return true;
  const left = halves[0] ? halves[0].split(":").filter(Boolean) : [];
  const right = halves[1] ? halves[1].split(":").filter(Boolean) : [];
  if (
    left.some((part) => !/^[0-9a-f]{1,4}$/.test(part)) ||
    right.some((part) => !/^[0-9a-f]{1,4}$/.test(part))
  )
    return true;
  const groups = [
    ...left,
    ...Array(8 - left.length - right.length).fill("0"),
    ...right,
  ];
  if (groups.length !== 8) return true;
  const first = Number.parseInt(groups[0], 16);
  const second = Number.parseInt(groups[1], 16);
  const isIpv4Compatible =
    first === 0 && groups.slice(1, 6).every((group) => group === "0");
  const isIpv4Mapped =
    first === 0 &&
    groups.slice(1, 5).every((group) => group === "0") &&
    groups[5].toLowerCase() === "ffff";
  const mappedIpv4 = isIpv4Mapped
    ? [
        Number.parseInt(groups[6], 16) >> 8,
        Number.parseInt(groups[6], 16) & 0xff,
        Number.parseInt(groups[7], 16) >> 8,
        Number.parseInt(groups[7], 16) & 0xff,
      ].join(".")
    : null;
  return (
    (first === 0 && groups.slice(1).every((group) => group === "0")) ||
    (isIpv4Compatible && (groups[7] === "0" || groups[7] === "1")) ||
    (mappedIpv4 !== null && isPrivateIpLiteral(mappedIpv4)) ||
    (first & 0xfe00) === 0xfc00 ||
    (first & 0xffc0) === 0xfe80 ||
    (first & 0xff00) === 0xff00 ||
    (first === 0x2001 && second === 0x0db8)
  );
}

function normalizeScopes(value: unknown): string[] {
  if (value === undefined || value === null || value === "") return [];
  if (!Array.isArray(value) && typeof value !== "string")
    throw new McpAppOauthError(422, "invalid_scope");
  const values = Array.isArray(value) ? value : value.split(/[\s,]+/);
  if (values.some((scope) => typeof scope !== "string"))
    throw new McpAppOauthError(422, "invalid_scope");
  const scopes = values
    .map((scope) => String(scope))
    .map((scope) => scope.trim())
    .filter(Boolean);
  if (
    scopes.length > MAX_SCOPES ||
    scopes.some(
      (scope) => utf8Bytes(scope) > 128 || !/^[A-Za-z0-9._:-]+$/.test(scope),
    )
  )
    throw new McpAppOauthError(422, "invalid_scope");
  return [...new Set(scopes)];
}

function validatedCredential(
  value: unknown,
  code: string,
  status: 401 | 422 | 502,
  required = false,
): string | null {
  if (value === undefined || value === null) {
    if (required) throw new McpAppOauthError(status, code);
    return null;
  }
  if (
    typeof value !== "string" ||
    utf8Bytes(value) === 0 ||
    utf8Bytes(value) > MAX_CREDENTIAL_BYTES ||
    /[\u0000-\u001f\u007f]/.test(value)
  )
    throw new McpAppOauthError(status, code);
  return value;
}

function parseBody(raw: string): JsonObject {
  if (utf8Bytes(raw) > MAX_BODY_BYTES)
    throw new McpAppOauthError(413, "request_too_large");
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new McpAppOauthError(400, "invalid_request");
  }
  const object = objectValue(value);
  if (!object) throw new McpAppOauthError(400, "invalid_request");
  return object;
}

async function boundedJson(
  response: Response,
  unavailableCode: string,
): Promise<JsonObject> {
  const contentLength = Number(response.headers.get("content-length") || "0");
  if (
    Number.isFinite(contentLength) &&
    contentLength > MAX_PROVIDER_RESPONSE_BYTES
  )
    throw new McpAppOauthError(502, unavailableCode);
  const raw = await readBoundedText(
    response.body,
    MAX_PROVIDER_RESPONSE_BYTES,
    unavailableCode,
  );
  let decoded: unknown;
  try {
    decoded = JSON.parse(raw);
  } catch {
    throw new McpAppOauthError(502, unavailableCode);
  }
  const parsed = objectValue(decoded);
  if (!parsed) throw new McpAppOauthError(502, unavailableCode);
  return parsed;
}

function parseSseJson(raw: string): JsonObject | null {
  let last: JsonObject | null = null;
  for (const line of raw.split(/\r?\n/)) {
    if (!line.startsWith("data:")) continue;
    const value = line.slice(5).trim();
    if (!value || value === "[DONE]") continue;
    try {
      const parsed = objectValue(JSON.parse(value));
      if (parsed) last = parsed;
    } catch {
      // Ignore non-JSON SSE keepalives and continue to the bounded final event.
    }
  }
  return last;
}

async function boundedRpcJson(
  response: Response,
  unavailableCode: string,
): Promise<JsonObject> {
  const contentLength = Number(response.headers.get("content-length") || "0");
  if (
    Number.isFinite(contentLength) &&
    contentLength > MAX_PROVIDER_RESPONSE_BYTES
  )
    throw new McpAppOauthError(502, unavailableCode);
  const raw = await readBoundedText(
    response.body,
    MAX_PROVIDER_RESPONSE_BYTES,
    unavailableCode,
  );
  const contentType = response.headers.get("content-type")?.toLowerCase() || "";
  let payload: JsonObject | null = null;
  if (contentType.includes("text/event-stream")) payload = parseSseJson(raw);
  if (!payload) {
    try {
      payload = objectValue(JSON.parse(raw));
    } catch {
      payload = null;
    }
  }
  if (!payload) throw new McpAppOauthError(502, unavailableCode);
  return payload;
}

function strictRpcResult(payload: JsonObject, expectedId: number): JsonObject {
  if (payload.jsonrpc !== "2.0" || payload.id !== expectedId)
    throw new McpAppOauthError(502, "discovery_response_invalid");
  const hasResult = Object.prototype.hasOwnProperty.call(payload, "result");
  const hasError = Object.prototype.hasOwnProperty.call(payload, "error");
  if (hasResult === hasError)
    throw new McpAppOauthError(502, "discovery_response_invalid");
  if (hasError) {
    const error = objectValue(payload.error);
    if (
      !error ||
      typeof error.code !== "number" ||
      !Number.isSafeInteger(error.code) ||
      typeof error.message !== "string" ||
      utf8Bytes(error.message) > 8_192
    )
      throw new McpAppOauthError(502, "discovery_response_invalid");
    throw new McpAppOauthError(502, "discovery_provider_error");
  }
  const result = objectValue(payload.result);
  if (!result) throw new McpAppOauthError(502, "discovery_response_invalid");
  return result;
}

function strictToolCallResult(payload: JsonObject, expectedId: number): JsonObject {
  if (payload.jsonrpc !== "2.0" || payload.id !== expectedId)
    throw new McpAppOauthError(502, "mcp_call_response_invalid");
  const hasResult = Object.prototype.hasOwnProperty.call(payload, "result");
  const hasError = Object.prototype.hasOwnProperty.call(payload, "error");
  if (hasResult === hasError)
    throw new McpAppOauthError(502, "mcp_call_response_invalid");
  if (hasError) {
    const error = objectValue(payload.error);
    if (
      !error ||
      typeof error.code !== "number" ||
      !Number.isSafeInteger(error.code) ||
      typeof error.message !== "string" ||
      utf8Bytes(error.message) > 8_192
    )
      throw new McpAppOauthError(502, "mcp_call_response_invalid");
    throw new McpAppOauthError(502, "mcp_provider_error");
  }
  const result = objectValue(payload.result);
  if (!result) throw new McpAppOauthError(502, "mcp_call_response_invalid");
  return result;
}

const MAX_CALL_ARGUMENT_BYTES = 16_000;
const MAX_CALL_ARGUMENT_DEPTH = 16;
const MAX_CALL_ARGUMENT_PROPERTIES = 256;

function validateCallArguments(value: unknown, depth = 0, properties = 0): number {
  if (depth > MAX_CALL_ARGUMENT_DEPTH)
    throw new McpAppOauthError(422, "invalid_tool_arguments");
  if (value === null || typeof value !== "object") return properties;
  if (Array.isArray(value)) {
    if (value.length > MAX_CALL_ARGUMENT_PROPERTIES)
      throw new McpAppOauthError(422, "invalid_tool_arguments");
    let total = properties;
    for (const item of value)
      total = validateCallArguments(item, depth + 1, total);
    return total;
  }
  const object = value as Record<string, unknown>;
  const keys = Object.keys(object);
  if (keys.length > MAX_CALL_ARGUMENT_PROPERTIES)
    throw new McpAppOauthError(422, "invalid_tool_arguments");
  let total = properties + keys.length;
  if (total > MAX_CALL_ARGUMENT_PROPERTIES)
    throw new McpAppOauthError(422, "invalid_tool_arguments");
  for (const item of Object.values(object))
    total = validateCallArguments(item, depth + 1, total);
  return total;
}

function nextToolsCursor(result: JsonObject): string | null {
  if (result.nextCursor === undefined || result.nextCursor === null)
    return null;
  if (
    typeof result.nextCursor !== "string" ||
    !result.nextCursor ||
    utf8Bytes(result.nextCursor) > MAX_CURSOR_BYTES ||
    /[\u0000-\u001f\u007f]/.test(result.nextCursor)
  )
    throw new McpAppOauthError(502, "discovery_response_invalid");
  return result.nextCursor;
}

type SseReaderState = {
  reader: ReadableStreamDefaultReader<Uint8Array>;
  decoder: TextDecoder;
  buffer: string;
  event: string;
  data: string[];
  totalBytes: number;
};

type SseEvent = { event: string; data: string };

async function nextSseEvent(state: SseReaderState): Promise<SseEvent | null> {
  const dispatch = (): SseEvent | null => {
    if (!state.data.length) {
      state.event = "";
      return null;
    }
    const event = {
      event: state.event || "message",
      data: state.data.join("\n"),
    };
    state.event = "";
    state.data = [];
    return event;
  };
  while (true) {
    const newline = state.buffer.indexOf("\n");
    if (newline >= 0) {
      let line = state.buffer.slice(0, newline);
      state.buffer = state.buffer.slice(newline + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      if (!line) {
        const event = dispatch();
        if (event) return event;
        continue;
      }
      if (line.startsWith(":")) continue;
      const separator = line.indexOf(":");
      const field = separator < 0 ? line : line.slice(0, separator);
      const value =
        separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
      if (field === "event") state.event = value.slice(0, 128);
      if (field === "data") {
        state.data.push(value);
        if (utf8Bytes(state.data.join("\n")) > MAX_PROVIDER_RESPONSE_BYTES)
          throw new McpAppOauthError(502, "discovery_response_invalid");
      }
      continue;
    }
    const next = await state.reader.read();
    if (next.done) {
      state.buffer += state.decoder.decode();
      if (state.buffer) {
        state.buffer += "\n";
        continue;
      }
      const event = dispatch();
      return event;
    }
    state.totalBytes += next.value.byteLength;
    if (state.totalBytes > MAX_PROVIDER_RESPONSE_BYTES) {
      await state.reader.cancel();
      throw new McpAppOauthError(502, "discovery_response_invalid");
    }
    state.buffer += state.decoder.decode(next.value, { stream: true });
  }
}

function sseJsonRpc(event: SseEvent): JsonObject {
  let payload: unknown;
  try {
    payload = JSON.parse(event.data);
  } catch {
    throw new McpAppOauthError(502, "discovery_response_invalid");
  }
  const object = objectValue(payload);
  if (!object) throw new McpAppOauthError(502, "discovery_response_invalid");
  return object;
}

function legacySseEndpoint(base: string, value: string): string {
  let resolved: URL;
  try {
    resolved = new URL(value, base);
  } catch {
    throw new McpAppOauthError(502, "discovery_response_invalid");
  }
  if (!publicHttps(resolved.toString()))
    throw new McpAppOauthError(502, "discovery_response_invalid");
  const baseUrl = new URL(base);
  if (resolved.origin !== baseUrl.origin)
    throw new McpAppOauthError(502, "discovery_response_invalid");
  return resolved.toString();
}

type DiscoveryTransportResult = {
  endpoint: string;
  transport: "streamable_http" | "sse";
  protocolVersion: string;
  tools: JsonObject[];
  etag: string | null;
};

type LegacySseSession = {
  endpoint: string;
  state: SseReaderState;
};

async function openLegacySse(
  dependencies: McpAppOauthDependencies,
  endpoint: string,
  accessToken: string,
): Promise<LegacySseSession> {
  const response = await providerFetch(dependencies, endpoint, {
    method: "GET",
    headers: {
      accept: "text/event-stream",
      ...optionalBearer(accessToken),
    },
  });
  if (response.status === 401)
    throw new McpAppOauthError(401, "mcp_reauthorization_required");
  if (!response.ok) throw new McpAppOauthError(502, "discovery_unavailable");
  if (!response.body)
    throw new McpAppOauthError(502, "discovery_response_invalid");
  const contentType = response.headers.get("content-type")?.toLowerCase() || "";
  if (!contentType.includes("text/event-stream"))
    throw new McpAppOauthError(502, "discovery_response_invalid");
  const state: SseReaderState = {
    reader: response.body.getReader(),
    decoder: new TextDecoder(),
    buffer: "",
    event: "",
    data: [],
    totalBytes: 0,
  };
  for (let count = 0; count < 32; count += 1) {
    const event = await nextSseEvent(state);
    if (!event) break;
    if (event.event !== "endpoint") continue;
    const value = event.data.trim();
    if (!value || utf8Bytes(value) > MAX_ENDPOINT_LENGTH)
      throw new McpAppOauthError(502, "discovery_response_invalid");
    return { endpoint: legacySseEndpoint(endpoint, value), state };
  }
  await state.reader.cancel();
  throw new McpAppOauthError(502, "discovery_response_invalid");
}

async function legacySseRpc(
  dependencies: McpAppOauthDependencies,
  session: LegacySseSession,
  headers: Record<string, string>,
  request: JsonObject,
  expectedId: number,
  resultParser: (payload: JsonObject, expectedId: number) => JsonObject =
    strictRpcResult,
): Promise<JsonObject> {
  const response = await providerFetch(dependencies, session.endpoint, {
    method: "POST",
    headers: { ...headers, accept: "application/json, text/event-stream" },
    body: JSON.stringify(request),
  });
  if (response.status === 401)
    throw new McpAppOauthError(401, "mcp_reauthorization_required");
  if (!response.ok && response.status !== 202 && response.status !== 204)
    throw new McpAppOauthError(502, "discovery_unavailable");
  if (response.status !== 202 && response.status !== 204) {
    const payload = await boundedRpcJson(
      response,
      "discovery_response_invalid",
    );
    return resultParser(payload, expectedId);
  }
  for (let count = 0; count < 32; count += 1) {
    const event = await nextSseEvent(session.state);
    if (!event) break;
    if (event.event === "endpoint") continue;
    const payload = sseJsonRpc(event);
    if (payload.id !== expectedId) continue;
    return resultParser(payload, expectedId);
  }
  throw new McpAppOauthError(502, "discovery_response_invalid");
}

async function discoverSse(
  dependencies: McpAppOauthDependencies,
  endpoint: string,
  accessToken: string,
): Promise<DiscoveryTransportResult> {
  const session = await openLegacySse(dependencies, endpoint, accessToken);
  const headers = {
    "content-type": "application/json",
    ...optionalBearer(accessToken),
  };
  const initialize = await legacySseRpc(
    dependencies,
    session,
    headers,
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: MCP_PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: "Omi", version: "1.0.0" },
      },
    },
    1,
  );
  const protocolVersion = initialize.protocolVersion;
  if (
    typeof protocolVersion !== "string" ||
    !SUPPORTED_MCP_PROTOCOL_VERSIONS.has(protocolVersion)
  )
    throw new McpAppOauthError(502, "discovery_response_invalid");
  const notification = await providerFetch(dependencies, session.endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify({
      jsonrpc: "2.0",
      method: "notifications/initialized",
    }),
  });
  if (
    !notification.ok &&
    notification.status !== 202 &&
    notification.status !== 204
  )
    throw new McpAppOauthError(502, "discovery_unavailable");
  let cursor: string | null = null;
  const cursors = new Set<string>();
  const tools: JsonObject[] = [];
  const toolNames = new Set<string>();
  let etag: string | null = null;
  for (let page = 0; page < MAX_TOOLS_PAGES; page += 1) {
    const result = await legacySseRpc(
      dependencies,
      session,
      headers,
      {
        jsonrpc: "2.0",
        id: page + 2,
        method: "tools/list",
        params: cursor ? { cursor } : {},
      },
      page + 2,
    );
    const pageTools = validateMcpTools(result.tools);
    for (const tool of pageTools) {
      const name = String(tool.name);
      if (toolNames.has(name))
        throw new McpAppOauthError(502, "discovery_response_invalid");
      toolNames.add(name);
    }
    tools.push(...pageTools);
    if (tools.length > MAX_TOOLS)
      throw new McpAppOauthError(502, "discovery_response_invalid");
    const nextCursor = nextToolsCursor(result);
    if (!nextCursor) break;
    if (cursors.has(nextCursor))
      throw new McpAppOauthError(502, "discovery_response_invalid");
    cursors.add(nextCursor);
    cursor = nextCursor;
    if (page === MAX_TOOLS_PAGES - 1)
      throw new McpAppOauthError(502, "discovery_response_invalid");
  }
  if (!tools.length) throw new McpAppOauthError(422, "no_tools_found");
  return {
    endpoint,
    transport: "sse",
    protocolVersion,
    tools,
    etag,
  };
}

async function discoverStreamableHttp(
  dependencies: McpAppOauthDependencies,
  endpoint: string,
  accessToken: string,
): Promise<DiscoveryTransportResult> {
  const headers = {
    "content-type": "application/json",
    accept: "application/json, text/event-stream",
    ...optionalBearer(accessToken),
  };
  const initialize = await providerFetch(dependencies, endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: MCP_PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: "Omi", version: "1.0.0" },
      },
    }),
  });
  if (initialize.status === 401)
    throw new McpAppOauthError(401, "mcp_reauthorization_required");
  if (initialize.status === 404 || initialize.status === 405)
    throw new McpAppOauthError(502, "streamable_transport_unavailable");
  if (!initialize.ok) throw new McpAppOauthError(502, "discovery_unavailable");
  const initializePayload = await boundedRpcJson(
    initialize,
    "discovery_response_invalid",
  );
  const initializeResult = strictRpcResult(initializePayload, 1);
  const protocolVersion = initializeResult.protocolVersion;
  if (
    typeof protocolVersion !== "string" ||
    !SUPPORTED_MCP_PROTOCOL_VERSIONS.has(protocolVersion)
  )
    throw new McpAppOauthError(502, "discovery_response_invalid");
  const sessionId = initialize.headers.get("mcp-session-id");
  const sessionHeaders = {
    ...headers,
    ...(sessionId ? { "mcp-session-id": sessionId } : {}),
  };
  const notification = await providerFetch(dependencies, endpoint, {
    method: "POST",
    headers: sessionHeaders,
    body: JSON.stringify({
      jsonrpc: "2.0",
      method: "notifications/initialized",
    }),
  });
  if (
    !notification.ok &&
    notification.status !== 202 &&
    notification.status !== 204
  )
    throw new McpAppOauthError(502, "discovery_unavailable");
  let cursor: string | null = null;
  const cursors = new Set<string>();
  const tools: JsonObject[] = [];
  const toolNames = new Set<string>();
  let toolsResponse: Response | null = null;
  for (let page = 0; page < MAX_TOOLS_PAGES; page += 1) {
    const requestId = page + 2;
    toolsResponse = await providerFetch(dependencies, endpoint, {
      method: "POST",
      headers: sessionHeaders,
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: requestId,
        method: "tools/list",
        params: cursor ? { cursor } : {},
      }),
    });
    if (toolsResponse.status === 401)
      throw new McpAppOauthError(401, "mcp_reauthorization_required");
    if (!toolsResponse.ok)
      throw new McpAppOauthError(502, "discovery_unavailable");
    const toolsPayload = await boundedRpcJson(
      toolsResponse,
      "discovery_response_invalid",
    );
    const toolsResult = strictRpcResult(toolsPayload, requestId);
    const pageTools = validateMcpTools(toolsResult.tools);
    for (const tool of pageTools) {
      const name = String(tool.name);
      if (toolNames.has(name))
        throw new McpAppOauthError(502, "discovery_response_invalid");
      toolNames.add(name);
    }
    tools.push(...pageTools);
    if (tools.length > MAX_TOOLS)
      throw new McpAppOauthError(502, "discovery_response_invalid");
    const nextCursor = nextToolsCursor(toolsResult);
    if (!nextCursor) break;
    if (cursors.has(nextCursor))
      throw new McpAppOauthError(502, "discovery_response_invalid");
    cursors.add(nextCursor);
    cursor = nextCursor;
    if (page === MAX_TOOLS_PAGES - 1)
      throw new McpAppOauthError(502, "discovery_response_invalid");
  }
  if (!tools.length) throw new McpAppOauthError(422, "no_tools_found");
  return {
    endpoint,
    transport: "streamable_http",
    protocolVersion,
    tools,
    etag: toolsResponse?.headers.get("etag") || null,
  };
}

async function discoverEndpoint(
  dependencies: McpAppOauthDependencies,
  endpoints: string[],
  accessToken: string,
): Promise<DiscoveryTransportResult> {
  let lastError: unknown = null;
  for (const endpoint of endpoints) {
    try {
      return await discoverStreamableHttp(dependencies, endpoint, accessToken);
    } catch (error) {
      if (
        error instanceof McpAppOauthError &&
        (error.status === 401 || error.code === "mcp_reauthorization_required")
      )
        throw error;
      lastError = error;
      if (
        error instanceof McpAppOauthError &&
        error.code === "streamable_transport_unavailable"
      ) {
        try {
          return await discoverSse(dependencies, endpoint, accessToken);
        } catch (sseError) {
          if (sseError instanceof McpAppOauthError && sseError.status === 401)
            throw sseError;
          lastError = sseError;
        }
      }
    }
  }
  if (lastError) throw lastError;
  throw new McpAppOauthError(502, "discovery_unavailable");
}

async function callStreamableHttp(
  dependencies: McpAppOauthDependencies,
  endpoint: string,
  accessToken: string,
  toolName: string,
  args: JsonObject,
): Promise<JsonObject | null> {
  const headers = {
    "content-type": "application/json",
    accept: "application/json, text/event-stream",
    ...optionalBearer(accessToken),
  };
  const initialize = await providerFetch(dependencies, endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: MCP_PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: "Omi", version: "1.0.0" },
      },
    }),
  });
  if (initialize.status === 401)
    throw new McpAppOauthError(401, "mcp_reauthorization_required");
  if (initialize.status === 404 || initialize.status === 405) return null;
  if (!initialize.ok)
    throw new McpAppOauthError(502, "mcp_provider_unavailable");
  const initializeResult = strictToolCallResult(
    await boundedRpcJson(initialize, "mcp_call_response_invalid"),
    1,
  );
  if (
    typeof initializeResult.protocolVersion !== "string" ||
    !SUPPORTED_MCP_PROTOCOL_VERSIONS.has(initializeResult.protocolVersion)
  )
    throw new McpAppOauthError(502, "mcp_call_response_invalid");
  const sessionId = initialize.headers.get("mcp-session-id");
  if (sessionId && (utf8Bytes(sessionId) > MAX_CURSOR_BYTES || /[\u0000-\u001f\u007f]/.test(sessionId)))
    throw new McpAppOauthError(502, "mcp_call_response_invalid");
  const sessionHeaders = {
    ...headers,
    ...(sessionId ? { "mcp-session-id": sessionId } : {}),
  };
  const notification = await providerFetch(dependencies, endpoint, {
    method: "POST",
    headers: sessionHeaders,
    body: JSON.stringify({
      jsonrpc: "2.0",
      method: "notifications/initialized",
    }),
  });
  if (!notification.ok && notification.status !== 202 && notification.status !== 204)
    throw new McpAppOauthError(502, "mcp_provider_unavailable");
  const response = await providerFetch(dependencies, endpoint, {
    method: "POST",
    headers: sessionHeaders,
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: toolName, arguments: args },
    }),
  });
  if (response.status === 401)
    throw new McpAppOauthError(401, "mcp_reauthorization_required");
  if (!response.ok)
    throw new McpAppOauthError(502, "mcp_provider_unavailable");
  return strictToolCallResult(
    await boundedRpcJson(response, "mcp_call_response_invalid"),
    1,
  );
}

async function callLegacySse(
  dependencies: McpAppOauthDependencies,
  endpoint: string,
  accessToken: string,
  toolName: string,
  args: JsonObject,
): Promise<JsonObject> {
  const session = await openLegacySse(dependencies, endpoint, accessToken);
  const headers = {
    "content-type": "application/json",
    ...optionalBearer(accessToken),
  };
  try {
    await legacySseRpc(
      dependencies,
      session,
      headers,
      {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: MCP_PROTOCOL_VERSION,
          capabilities: {},
          clientInfo: { name: "Omi", version: "1.0.0" },
        },
      },
      1,
      strictToolCallResult,
    );
    const notification = await providerFetch(dependencies, session.endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify({
        jsonrpc: "2.0",
        method: "notifications/initialized",
      }),
    });
    if (!notification.ok && notification.status !== 202 && notification.status !== 204)
      throw new McpAppOauthError(502, "mcp_provider_unavailable");
    return await legacySseRpc(
      dependencies,
      session,
      headers,
      {
        jsonrpc: "2.0",
        id: 1,
        method: "tools/call",
        params: { name: toolName, arguments: args },
      },
      1,
      strictToolCallResult,
    );
  } finally {
    try {
      await session.state.reader.cancel();
    } catch {
      // The provider stream may already be closed after delivering the result.
    }
  }
}

async function readBoundedText(
  body: ReadableStream<Uint8Array> | null,
  limit: number,
  unavailableCode: string,
): Promise<string> {
  if (!body) return "";
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      total += next.value.byteLength;
      if (total > limit) {
        await reader.cancel();
        throw new McpAppOauthError(502, unavailableCode);
      }
      chunks.push(next.value);
    }
  } catch (error) {
    if (error instanceof McpAppOauthError) throw error;
    throw new McpAppOauthError(502, unavailableCode);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(bytes);
}

async function requestText(c: JobsContext): Promise<string> {
  const contentLength = Number(c.req.header("content-length") || "0");
  if (Number.isFinite(contentLength) && contentLength > MAX_BODY_BYTES)
    throw new McpAppOauthError(413, "request_too_large");
  return readBoundedText(c.req.raw.body, MAX_BODY_BYTES, "request_too_large");
}

function errorResponse(c: JobsContext, error: unknown): Response {
  if (error instanceof McpAppOauthError)
    return c.json({ error: error.code }, error.status);
  return c.json({ error: "mcp_app_oauth_unavailable" }, 503);
}

function validateMcpTools(value: unknown): JsonObject[] {
  if (!Array.isArray(value) || value.length > MAX_TOOLS)
    throw new McpAppOauthError(502, "discovery_response_invalid");
  const tools: JsonObject[] = [];
  const names = new Set<string>();
  for (const rawTool of value) {
    const tool = objectValue(rawTool);
    const name = tool?.name;
    if (
      !tool ||
      typeof name !== "string" ||
      !name.trim() ||
      utf8Bytes(name) > MAX_TOOL_NAME_BYTES ||
      /[\u0000-\u001f\u007f]/.test(name)
    )
      throw new McpAppOauthError(502, "discovery_response_invalid");
    const normalizedName = name.trim();
    if (names.has(normalizedName))
      throw new McpAppOauthError(502, "discovery_response_invalid");
    names.add(normalizedName);
    const description = tool.description;
    if (
      description !== undefined &&
      (typeof description !== "string" ||
        utf8Bytes(description) > MAX_TOOL_DESCRIPTION_BYTES)
    )
      throw new McpAppOauthError(502, "discovery_response_invalid");
    if (tool.inputSchema !== undefined) {
      if (!objectValue(tool.inputSchema))
        throw new McpAppOauthError(502, "discovery_response_invalid");
      const schemaJson = JSON.stringify(tool.inputSchema);
      if (utf8Bytes(schemaJson) > MAX_TOOL_SCHEMA_BYTES)
        throw new McpAppOauthError(502, "discovery_response_invalid");
      validateToolSchema(tool.inputSchema);
    }
    tools.push({
      name: normalizedName,
      ...(typeof description === "string" ? { description } : {}),
      ...(tool.inputSchema !== undefined
        ? { inputSchema: tool.inputSchema }
        : {}),
    });
  }
  const serialized = JSON.stringify(tools);
  if (utf8Bytes(serialized) > 2_000_000)
    throw new McpAppOauthError(502, "discovery_response_invalid");
  return tools;
}

function validateToolSchema(value: unknown, depth = 0, properties = 0): number {
  if (depth > MAX_TOOL_SCHEMA_DEPTH)
    throw new McpAppOauthError(502, "discovery_response_invalid");
  if (value === null || typeof value !== "object") return properties;
  if (Array.isArray(value)) {
    if (value.length > MAX_TOOL_SCHEMA_PROPERTIES)
      throw new McpAppOauthError(502, "discovery_response_invalid");
    return value.reduce(
      (count, item) => validateToolSchema(item, depth + 1, count),
      properties,
    );
  }
  const object = value as Record<string, unknown>;
  const keys = Object.keys(object);
  if (keys.length > MAX_TOOL_SCHEMA_PROPERTIES)
    throw new McpAppOauthError(502, "discovery_response_invalid");
  let count = properties + keys.length;
  if (count > MAX_TOOL_SCHEMA_PROPERTIES)
    throw new McpAppOauthError(502, "discovery_response_invalid");
  for (const key of keys)
    count = validateToolSchema(object[key], depth + 1, count);
  return count;
}

function callbackHtml(title: string, message: string): string {
  const escape = (value: string) =>
    value.replace(
      /[&<>"']/g,
      (character) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[character] || character,
    );
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none';style-src 'unsafe-inline'"><title>${escape(title)}</title></head><body><h1>${escape(title)}</h1><p>${escape(message)}</p></body></html>`;
}

function callbackResponse(
  c: JobsContext,
  status: 200 | 400 | 404 | 409 | 502 | 503,
  title: string,
  message: string,
): Response {
  c.header("cache-control", "no-store");
  c.header(
    "content-security-policy",
    "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
  );
  return c.html(callbackHtml(title, message), status);
}

function callbackUri(
  env: JobsEnv,
  surface: McpAppOauthSurface = "namespaced",
): string {
  const base = env.PUBLIC_API_BASE_URL?.trim();
  if (!publicHttps(base))
    throw new McpAppOauthError(503, "mcp_app_oauth_unavailable");
  return new URL(
    surface === "legacy"
      ? "/v1/apps/mcp/callback"
      : "/v2/cf/apps/mcp/callback",
    `${base.replace(/\/$/, "")}/`,
  ).toString();
}

async function providerFetch(
  dependencies: McpAppOauthDependencies,
  input: RequestInfo | URL,
  init: RequestInit,
): Promise<Response> {
  try {
    return await (dependencies.fetchImpl || fetch)(input, {
      ...init,
      redirect: "error",
      signal: AbortSignal.timeout(20_000),
    });
  } catch {
    throw new McpAppOauthError(503, "provider_unavailable");
  }
}

function optionalBearer(accessToken: string): Record<string, string> {
  return accessToken ? { authorization: `Bearer ${accessToken}` } : {};
}

function surfaceEnabled(
  env: JobsEnv,
  surface: McpAppOauthSurface,
): boolean {
  return surface === "legacy"
    ? env.MCP_APP_LEGACY_EXACT_STAGING_ENABLED === "true"
    : env.MCP_APP_OAUTH_STAGING_ENABLED === "true";
}

async function start(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: McpAppOauthDependencies,
  surface: McpAppOauthSurface = "namespaced",
): Promise<Response> {
  if (!surfaceEnabled(c.env, surface))
    throw new McpAppOauthError(404, "not_found");
  const body = parseBody(await requestText(c));
  return startWithBody(c, context, dependencies, body, surface);
}

async function startWithBody(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: McpAppOauthDependencies,
  body: JsonObject,
  surface: McpAppOauthSurface,
): Promise<Response> {
  if (!surfaceEnabled(c.env, surface))
    throw new McpAppOauthError(404, "not_found");
  const appId = typeof body.app_id === "string" ? body.app_id.trim() : "";
  const serverUrl =
    typeof body.server_url === "string"
      ? body.server_url.trim().replace(/\/$/, "")
      : "";
  const authorizationEndpoint = body.authorization_endpoint;
  const tokenEndpoint = body.token_endpoint;
  const registrationEndpoint = body.registration_endpoint;
  if (
    !appId ||
    appId.length > 256 ||
    !publicHttps(serverUrl) ||
    !publicHttps(authorizationEndpoint) ||
    !publicHttps(tokenEndpoint)
  )
    throw new McpAppOauthError(422, "invalid_provider_metadata");
  if (registrationEndpoint !== undefined && !publicHttps(registrationEndpoint))
    throw new McpAppOauthError(422, "invalid_provider_metadata");
  const endpointCandidates = normalizeEndpointCandidates(
    serverUrl,
    body.endpoint_candidates,
  );
  const scopes = normalizeScopes(body.scopes);
  const app = await c.env.APP_DB.prepare(
    "SELECT id, owner_uid, disabled FROM cf_app_catalog WHERE id = ? AND owner_uid = ? LIMIT 1",
  )
    .bind(appId, context.uid)
    .first<{ id?: unknown; owner_uid?: unknown; disabled?: unknown }>();
  if (!app || Number(app.disabled) === 1)
    throw new McpAppOauthError(404, "app_not_found");
  const existingConnection = await c.env.APP_DB.prepare(
    "SELECT owner_uid FROM cf_mcp_app_connections WHERE app_id = ? LIMIT 1",
  )
    .bind(appId)
    .first<{ owner_uid?: unknown }>();
  if (existingConnection && existingConnection.owner_uid !== context.uid)
    throw new McpAppOauthError(409, "app_connection_owner_mismatch");
  const redirectUri = callbackUri(c.env, surface);
  let clientId =
    typeof body.client_id === "string" ? body.client_id.trim() : "";
  let clientSecret =
    typeof body.client_secret === "string" ? body.client_secret : null;
  if (clientId && (!CLIENT_ID_RE.test(clientId) || utf8Bytes(clientId) > 2_048))
    throw new McpAppOauthError(422, "invalid_client");
  clientSecret = validatedCredential(clientSecret, "invalid_client", 422);
  if (registrationEndpoint) {
    const registration = await providerFetch(
      dependencies,
      registrationEndpoint,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          accept: "application/json",
        },
        body: JSON.stringify({
          client_name: "Omi",
          redirect_uris: [redirectUri],
          token_endpoint_auth_method: "none",
          grant_types: ["authorization_code"],
          response_types: ["code"],
          ...(scopes.length ? { scope: scopes.join(" ") } : {}),
        }),
      },
    );
    if (!registration.ok)
      throw new McpAppOauthError(502, "registration_failed");
    const payload = await boundedJson(registration, "registration_invalid");
    clientId = typeof payload.client_id === "string" ? payload.client_id : "";
    clientSecret = validatedCredential(
      payload.client_secret,
      "registration_invalid",
      502,
    );
    if (!clientId || !CLIENT_ID_RE.test(clientId))
      throw new McpAppOauthError(502, "registration_invalid");
  }
  if (!clientId)
    throw new McpAppOauthError(422, "client_registration_required");
  const now = nowSeconds(dependencies);
  if (!now) throw new McpAppOauthError(503, "clock_unavailable");
  const transactionId = crypto.randomUUID();
  const state = randomOpaque();
  const verifier = randomOpaque();
  const challenge = base64Url(await sha256(verifier));
  const metadata = JSON.stringify({
    authorization_endpoint: authorizationEndpoint,
    token_endpoint: tokenEndpoint,
    registration_endpoint: registrationEndpoint || null,
    scopes,
    endpoint_candidates: endpointCandidates,
  });
  if (utf8Bytes(metadata) > MAX_METADATA_BYTES)
    throw new McpAppOauthError(422, "invalid_provider_metadata");
  const verifierEnvelope = await encrypt(
    c.env,
    "verifier",
    context.uid,
    appId,
    { verifier },
    transactionId,
  );
  const credentialsEnvelope = clientSecret
    ? await encrypt(
        c.env,
        "client_credentials",
        context.uid,
        appId,
        { client_secret: clientSecret },
        transactionId,
      )
    : null;
  await c.env.APP_DB.batch([
    c.env.APP_DB.prepare(
      `UPDATE cf_mcp_app_oauth_transactions
          SET status = 'expired', last_error = 'superseded', updated_at = ?
        WHERE app_id = ? AND owner_uid = ? AND status = 'pending'`,
    ).bind(now, appId, context.uid),
    c.env.APP_DB.prepare(
      `INSERT INTO cf_mcp_app_connections
         (app_id, owner_uid, server_url, status, oauth_metadata_json, credential_envelope_enc, oauth_transaction_id, revision, created_at, updated_at)
       VALUES (?, ?, ?, 'pending', ?, NULL, ?, 0, ?, ?)
       ON CONFLICT(app_id) DO UPDATE SET server_url = excluded.server_url,
         status = 'pending', oauth_metadata_json = excluded.oauth_metadata_json,
         credential_envelope_enc = NULL, last_error = NULL,
         oauth_transaction_id = excluded.oauth_transaction_id,
         updated_at = excluded.updated_at
       WHERE cf_mcp_app_connections.owner_uid = excluded.owner_uid`,
    ).bind(appId, context.uid, serverUrl, metadata, transactionId, now, now),
    c.env.APP_DB.prepare(
      `INSERT INTO cf_mcp_app_oauth_transactions
         (transaction_id, app_id, owner_uid, state_hash, code_verifier_enc, client_credentials_enc,
          authorization_endpoint, token_endpoint, registration_endpoint, client_id, redirect_uri,
          status, attempts, expires_at, consumed_at, last_error, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, ?)`,
    ).bind(
      transactionId,
      appId,
      context.uid,
      await sha256Hex(state),
      verifierEnvelope,
      credentialsEnvelope,
      authorizationEndpoint,
      tokenEndpoint,
      registrationEndpoint || null,
      clientId,
      redirectUri,
      now + STATE_TTL_SECONDS,
      now,
      now,
    ),
  ]);
  const url = new URL(authorizationEndpoint);
  url.search = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirectUri,
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
    ...(scopes.length ? { scope: scopes.join(" ") } : {}),
  }).toString();
  return c.json(
    { app_id: appId, requires_oauth: true, auth_url: url.toString() },
    200,
    { "cache-control": "no-store" },
  );
}

type LegacyMcpInput = {
  name: string;
  description: string;
  serverUrl: string;
};

function legacyMcpInput(body: JsonObject): LegacyMcpInput {
  const name = typeof body.name === "string" ? body.name.trim() : "";
  const descriptionValue = body.description;
  const description =
    descriptionValue === undefined || descriptionValue === null
      ? ""
      : typeof descriptionValue === "string"
        ? descriptionValue.trim()
        : "__invalid__";
  const serverUrl =
    typeof body.mcp_server_url === "string"
      ? body.mcp_server_url.trim().replace(/\/$/, "")
      : "";
  if (!name || utf8Bytes(name) > 160)
    throw new McpAppOauthError(422, "invalid_app_name");
  if (description === "__invalid__" || utf8Bytes(description) > 20_000)
    throw new McpAppOauthError(422, "invalid_app_description");
  if (!publicHttps(serverUrl))
    throw new McpAppOauthError(422, "invalid_provider_metadata");
  return { name, description, serverUrl };
}

async function discoverLegacyOauthMetadata(
  dependencies: McpAppOauthDependencies,
  serverUrl: string,
): Promise<{
  authorizationEndpoint: string;
  tokenEndpoint: string;
  registrationEndpoint?: string;
  scopes: string[];
} | null> {
  const metadataUrl = new URL(serverUrl);
  metadataUrl.pathname = "/.well-known/oauth-authorization-server";
  metadataUrl.search = "";
  metadataUrl.hash = "";
  const response = await providerFetch(dependencies, metadataUrl, {
    method: "GET",
    headers: { accept: "application/json" },
  });
  if (response.status === 404 || response.status === 405) return null;
  if (!response.ok) return null;
  const payload = await boundedJson(response, "oauth_metadata_invalid");
  const authorizationEndpoint = payload.authorization_endpoint;
  const tokenEndpoint = payload.token_endpoint;
  if (!publicHttps(authorizationEndpoint) || !publicHttps(tokenEndpoint))
    return null;
  const registrationEndpoint =
    payload.registration_endpoint === undefined
      ? undefined
      : payload.registration_endpoint;
  if (
    registrationEndpoint !== undefined &&
    !publicHttps(registrationEndpoint)
  )
    throw new McpAppOauthError(502, "oauth_metadata_invalid");
  const scopes = normalizeScopes(payload.scopes_supported);
  return {
    authorizationEndpoint,
    tokenEndpoint,
    ...(registrationEndpoint ? { registrationEndpoint } : {}),
    scopes,
  };
}

function legacyMcpAppId(): string {
  return `mcp-${randomOpaque(16)}`;
}

async function createLegacyMcpCatalogApp(
  c: JobsContext,
  context: SignedAuthContext,
  input: LegacyMcpInput,
  appId: string,
  now: number,
): Promise<void> {
  const payload = JSON.stringify({
    id: appId,
    name: input.name,
    description: input.description,
    image: "",
    uid: context.uid,
    private: true,
    approved: true,
    status: "pending_mcp_auth",
    category: "utilities-and-tools",
    capabilities: ["chat"],
    chat_tools: [],
    external_integration: { mcp_server_url: input.serverUrl },
  });
  await c.env.APP_DB.prepare(
    `INSERT INTO cf_app_catalog
       (id, approved, status, disabled, is_popular, installs, rating_avg,
        rating_count, data_json, updated_at, owner_uid)
     VALUES (?, 1, 'approved', 0, 0, 0, NULL, 0, ?, ?, ?)`,
  )
    .bind(appId, payload, now, context.uid)
    .run();
}

async function deleteLegacyMcpCatalogApp(
  c: JobsContext,
  context: SignedAuthContext,
  appId: string,
): Promise<void> {
  try {
    await c.env.APP_DB.prepare(
      "DELETE FROM cf_app_catalog WHERE id = ? AND owner_uid = ?",
    )
      .bind(appId, context.uid)
      .run();
  } catch {
    // Preserve the original provider error. Account deletion fences and a
    // concurrent app deletion must not turn it into a success response.
  }
}

async function createLegacyUnauthenticatedMcp(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: McpAppOauthDependencies,
  input: LegacyMcpInput,
): Promise<Response> {
  const now = nowSeconds(dependencies);
  if (!now) throw new McpAppOauthError(503, "clock_unavailable");
  const appId = legacyMcpAppId();
  await createLegacyMcpCatalogApp(c, context, input, appId, now);
  try {
    const envelope = await encrypt(
      c.env,
      "connection",
      context.uid,
      appId,
      { anonymous: true },
    );
    await c.env.APP_DB.prepare(
      `INSERT INTO cf_mcp_app_connections
         (app_id, owner_uid, server_url, status, oauth_metadata_json,
          credential_envelope_enc, revision, created_at, updated_at)
       VALUES (?, ?, ?, 'authorized', '{}', ?, 0, ?, ?)`,
    )
      .bind(appId, context.uid, input.serverUrl, envelope, now, now)
      .run();
    const discovery = await discover(c, context, dependencies, appId, "legacy");
    const payload = (await discovery.clone().json()) as JsonObject;
    const toolNames = Array.isArray(payload.tool_names)
      ? payload.tool_names.filter((value): value is string => typeof value === "string")
      : [];
    await install(c, context, dependencies, appId, "legacy");
    return c.json(
      {
        app_id: appId,
        requires_oauth: false,
        tools_count: toolNames.length,
        tool_names: toolNames,
      },
      200,
      { "cache-control": "no-store" },
    );
  } catch (error) {
    await deleteLegacyMcpCatalogApp(c, context, appId);
    throw error;
  }
}

async function startLegacy(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: McpAppOauthDependencies,
): Promise<Response> {
  if (!surfaceEnabled(c.env, "legacy"))
    throw new McpAppOauthError(404, "not_found");
  // Fail before metadata/provider I/O when the exact owner has not been
  // provisioned with its envelope key. This is the explicit staging boundary;
  // it must never turn a missing secret into a legacy fallback.
  encryptionSecret(c.env);
  const input = legacyMcpInput(parseBody(await requestText(c)));
  const metadata = await discoverLegacyOauthMetadata(
    dependencies,
    input.serverUrl,
  );
  if (!metadata) {
    return createLegacyUnauthenticatedMcp(c, context, dependencies, input);
  }
  if (!metadata.registrationEndpoint)
    throw new McpAppOauthError(422, "client_registration_required");
  const appId = legacyMcpAppId();
  const now = nowSeconds(dependencies);
  if (!now) throw new McpAppOauthError(503, "clock_unavailable");
  await createLegacyMcpCatalogApp(c, context, input, appId, now);
  try {
    return await startWithBody(
      c,
      context,
      dependencies,
      {
        app_id: appId,
        server_url: input.serverUrl,
        authorization_endpoint: metadata.authorizationEndpoint,
        token_endpoint: metadata.tokenEndpoint,
        registration_endpoint: metadata.registrationEndpoint,
        scopes: metadata.scopes,
      },
      "legacy",
    );
  } catch (error) {
    await deleteLegacyMcpCatalogApp(c, context, appId);
    throw error;
  }
}

async function callback(
  c: JobsContext,
  dependencies: McpAppOauthDependencies,
  surface: McpAppOauthSurface = "namespaced",
): Promise<Response> {
  if (!surfaceEnabled(c.env, surface))
    return callbackResponse(
      c,
      404,
      "Not found",
      "This staging seam is disabled.",
    );
  const state = c.req.query("state") || "";
  const code = c.req.query("code") || "";
  if (!OPAQUE_RE.test(state))
    return callbackResponse(
      c,
      400,
      "Authorization failed",
      "Invalid or expired state.",
    );
  const now = nowSeconds(dependencies);
  if (!now)
    return callbackResponse(
      c,
      503,
      "Authorization unavailable",
      "The authorization service is unavailable.",
    );
  const stateHash = await sha256Hex(state);
  const consumed = await c.env.APP_DB.prepare(
    `UPDATE cf_mcp_app_oauth_transactions
        SET status = 'exchanged', attempts = attempts + 1, consumed_at = ?, updated_at = ?
      WHERE state_hash = ? AND status = 'pending' AND expires_at > ?
      RETURNING transaction_id, app_id, owner_uid, code_verifier_enc, client_credentials_enc,
                token_endpoint, client_id, redirect_uri`,
  )
    .bind(now, now, stateHash, now)
    .first<{
      transaction_id?: string;
      app_id?: string;
      owner_uid?: string;
      code_verifier_enc?: string;
      client_credentials_enc?: string | null;
      token_endpoint?: string;
      client_id?: string;
      redirect_uri?: string;
    }>();
  if (
    !consumed?.transaction_id ||
    !consumed.app_id ||
    !consumed.owner_uid ||
    !consumed.code_verifier_enc ||
    !consumed.token_endpoint ||
    !consumed.client_id ||
    !consumed.redirect_uri
  )
    return callbackResponse(
      c,
      400,
      "Authorization failed",
      "Invalid or expired state.",
    );
  if (
    !code ||
    utf8Bytes(code) > MAX_CREDENTIAL_BYTES ||
    /[\u0000-\u001f\u007f]/.test(code)
  ) {
    await c.env.APP_DB.prepare(
      "UPDATE cf_mcp_app_oauth_transactions SET status = 'failed', last_error = ?, updated_at = ? WHERE transaction_id = ? AND status = 'exchanged'",
    )
      .bind("missing provider code", now, consumed.transaction_id)
      .run();
    return callbackResponse(
      c,
      400,
      "Authorization failed",
      "The provider callback did not include a valid code.",
    );
  }
  try {
    const verifierPayload = await decrypt(
      c.env,
      "verifier",
      consumed.owner_uid,
      consumed.app_id,
      consumed.code_verifier_enc,
      consumed.transaction_id,
    );
    const verifier = verifierPayload.verifier;
    if (typeof verifier !== "string" || !OPAQUE_RE.test(verifier))
      throw new Error("invalid verifier");
    let clientSecret: string | null = null;
    if (consumed.client_credentials_enc) {
      const credentials = await decrypt(
        c.env,
        "client_credentials",
        consumed.owner_uid,
        consumed.app_id,
        consumed.client_credentials_enc,
        consumed.transaction_id,
      );
      clientSecret =
        typeof credentials.client_secret === "string"
          ? credentials.client_secret
          : null;
    }
    const form = new URLSearchParams({
      grant_type: "authorization_code",
      code,
      client_id: consumed.client_id,
      redirect_uri: consumed.redirect_uri,
      code_verifier: verifier,
      ...(clientSecret ? { client_secret: clientSecret } : {}),
    });
    const response = await providerFetch(
      dependencies,
      consumed.token_endpoint,
      {
        method: "POST",
        headers: {
          "content-type": "application/x-www-form-urlencoded",
          accept: "application/json",
        },
        body: form.toString(),
      },
    );
    if (!response.ok) throw new McpAppOauthError(502, "token_exchange_failed");
    const payload = await boundedJson(response, "token_response_invalid");
    const accessToken = validatedCredential(
      payload.access_token,
      "token_response_invalid",
      502,
      true,
    ) as string;
    const refreshToken = validatedCredential(
      payload.refresh_token,
      "token_response_invalid",
      502,
    );
    const expiresIn = Number(payload.expires_in || 3_600);
    if (!Number.isSafeInteger(expiresIn) || expiresIn < 1 || expiresIn > 86_400)
      throw new McpAppOauthError(502, "token_response_invalid");
    const connectionEnvelope = await encrypt(
      c.env,
      "connection",
      consumed.owner_uid,
      consumed.app_id,
      {
        client_id: consumed.client_id,
        ...(clientSecret ? { client_secret: clientSecret } : {}),
        token_endpoint: consumed.token_endpoint,
        access_token: accessToken,
        refresh_token: refreshToken,
        expires_in: expiresIn,
        issued_at: now,
      },
    );
    const updated = await c.env.APP_DB.prepare(
      `UPDATE cf_mcp_app_connections
          SET status = 'authorized', credential_envelope_enc = ?, oauth_transaction_id = NULL, revision = revision + 1,
              last_error = NULL, updated_at = ?
        WHERE app_id = ? AND owner_uid = ? AND status = 'pending' AND oauth_transaction_id = ?`,
    )
      .bind(
        connectionEnvelope,
        now,
        consumed.app_id,
        consumed.owner_uid,
        consumed.transaction_id,
      )
      .run();
    if (Number(updated.meta?.changes) !== 1)
      throw new McpAppOauthError(409, "app_connection_changed");
    if (surface === "legacy") {
      const callbackContext = {
        uid: consumed.owner_uid,
        authority: "better-auth" as const,
        requestId: `mcp-callback-${consumed.transaction_id}`,
        version: 1 as const,
        audience: "jobs" as const,
        assertionId: consumed.transaction_id,
        issuedAt: now,
        expiresAt: now + 60,
        method: "GET",
        path: "/v1/apps/mcp/callback",
      } satisfies SignedAuthContext;
      // Released clients expect callback completion to include discovery and
      // auto-install.  The v2 seam keeps these as explicit follow-up calls;
      // this branch performs the same bounded operations before returning the
      // legacy HTML success page.
      await discover(c, callbackContext, dependencies, consumed.app_id, "legacy");
      await install(
        c,
        callbackContext,
        dependencies,
        consumed.app_id,
        "legacy",
      );
    }
    return callbackResponse(
      c,
      200,
      "Authorization complete",
      surface === "legacy"
        ? "The MCP server is connected and its tools are ready."
        : "The MCP server is authorized. Tool discovery is pending.",
    );
  } catch (error) {
    const codeValue =
      error instanceof McpAppOauthError ? error.code : "token_exchange_failed";
    try {
      await c.env.APP_DB.prepare(
        "UPDATE cf_mcp_app_oauth_transactions SET status = 'failed', last_error = ?, updated_at = ? WHERE transaction_id = ? AND status = 'exchanged'",
      )
        .bind(codeValue.slice(0, 2_000), now, consumed.transaction_id)
        .run();
    } catch {
      // Account-deletion fences intentionally reject every further mutation.
    }
    const status =
      error instanceof McpAppOauthError &&
      (error.status === 409 || error.status === 503 || error.status === 502)
        ? error.status
        : 502;
    return callbackResponse(
      c,
      status,
      "Authorization unavailable",
      "The provider authorization could not be completed.",
    );
  }
}

async function discover(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: McpAppOauthDependencies,
  appIdOverride?: string,
  surface: McpAppOauthSurface = "namespaced",
): Promise<Response> {
  if (!surfaceEnabled(c.env, surface))
    throw new McpAppOauthError(404, "not_found");
  const body = appIdOverride ? null : parseBody(await requestText(c));
  const appId =
    appIdOverride ||
    (typeof body?.app_id === "string" ? body.app_id.trim() : "");
  if (!appId || appId.length > 256)
    throw new McpAppOauthError(422, "invalid_request");
  const connection = await c.env.APP_DB.prepare(
    `SELECT c.app_id, c.owner_uid, c.server_url, c.resolved_endpoint, c.status,
            c.oauth_metadata_json, c.credential_envelope_enc, c.revision AS connection_revision,
            d.revision AS discovery_revision
       FROM cf_mcp_app_connections c
       LEFT JOIN cf_mcp_app_discoveries d ON d.app_id = c.app_id
      WHERE c.app_id = ? AND c.owner_uid = ?
      LIMIT 1`,
  )
    .bind(appId, context.uid)
    .first<{
      app_id?: string;
      owner_uid?: string;
      server_url?: string;
      resolved_endpoint?: string | null;
      status?: string;
      oauth_metadata_json?: string;
      credential_envelope_enc?: string | null;
      connection_revision?: number;
      discovery_revision?: number | null;
    }>();
  if (
    !connection?.app_id ||
    !connection.owner_uid ||
    connection.status !== "authorized" ||
    !connection.credential_envelope_enc
  )
    throw new McpAppOauthError(409, "mcp_authorization_required");
  const serverUrl = connection.server_url || "";
  const endpoints = metadataEndpointCandidates(
    serverUrl,
    connection.resolved_endpoint,
    connection.oauth_metadata_json,
  );
  if (!endpoints.length)
    throw new McpAppOauthError(409, "mcp_endpoint_unavailable");
  let endpoint = endpoints[0];
  const now = nowSeconds(dependencies);
  if (!now) throw new McpAppOauthError(503, "clock_unavailable");
  const observedConnectionRevision = Number(connection.connection_revision);
  if (
    !Number.isSafeInteger(observedConnectionRevision) ||
    observedConnectionRevision < 0
  )
    throw new McpAppOauthError(409, "app_connection_changed");
  const observedDiscoveryRevision =
    connection.discovery_revision === null ||
    connection.discovery_revision === undefined
      ? null
      : Number(connection.discovery_revision);
  if (
    observedDiscoveryRevision !== null &&
    (!Number.isSafeInteger(observedDiscoveryRevision) ||
      observedDiscoveryRevision < 0)
  )
    throw new McpAppOauthError(409, "discovery_changed");
  try {
    const credentials = await decrypt(
      c.env,
      "connection",
      connection.owner_uid,
      connection.app_id,
      connection.credential_envelope_enc,
    );
    const accessToken = validatedCredential(
      credentials.access_token,
      "mcp_authorization_required",
      401,
    ) || "";
    const discovery = await discoverEndpoint(
      dependencies,
      endpoints,
      accessToken,
    );
    endpoint = discovery.endpoint;
    const { protocolVersion, tools } = discovery;
    const toolsJson = JSON.stringify(tools);
    const connectionWrite = c.env.APP_DB.prepare(
      `UPDATE cf_mcp_app_connections
          SET resolved_endpoint = ?, revision = revision + 1, updated_at = ?
        WHERE app_id = ? AND owner_uid = ? AND status = 'authorized' AND revision = ?`,
    ).bind(endpoint, now, appId, context.uid, observedConnectionRevision);
    let discoveryWrite;
    if (observedDiscoveryRevision === null) {
      discoveryWrite = c.env.APP_DB.prepare(
        `INSERT INTO cf_mcp_app_discoveries
           (app_id, owner_uid, endpoint, protocol_version, tools_json,
            provider_etag, provider_session_id_enc, status, revision,
            last_error, fetched_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, NULL, 'ready', 0, NULL, ?, ?)
         ON CONFLICT(app_id) DO NOTHING`,
      ).bind(
        appId,
        context.uid,
        endpoint,
        protocolVersion,
        toolsJson,
        discovery.etag,
        now,
        now,
      );
    } else {
      discoveryWrite = c.env.APP_DB.prepare(
        `UPDATE cf_mcp_app_discoveries
            SET endpoint = ?, protocol_version = ?, tools_json = ?,
                provider_etag = ?, status = 'ready', revision = revision + 1,
                last_error = NULL, fetched_at = ?, updated_at = ?
          WHERE app_id = ? AND owner_uid = ? AND revision = ?`,
      ).bind(
        endpoint,
        protocolVersion,
        toolsJson,
        discovery.etag,
        now,
        now,
        appId,
        context.uid,
        observedDiscoveryRevision,
      );
    }
    const writes = await c.env.APP_DB.batch([connectionWrite, discoveryWrite]);
    if (Number(writes[0]?.meta?.changes) !== 1)
      throw new McpAppOauthError(409, "app_connection_changed");
    if (Number(writes[1]?.meta?.changes) !== 1)
      throw new McpAppOauthError(409, "discovery_changed");
    if (surface === "legacy") {
      const legacyTools = tools.map((tool) => ({
        name: String(tool.name),
        ...(typeof tool.description === "string"
          ? { description: tool.description }
          : {}),
        ...(tool.inputSchema !== undefined
          ? { parameters: tool.inputSchema }
          : {}),
      }));
      const catalogUpdate = await c.env.APP_DB.prepare(
        `UPDATE cf_app_catalog
            SET data_json = json_set(
              data_json,
              '$.status', 'approved',
              '$.chat_tools', json(?),
              '$.external_integration.mcp_server_url', ?
            ), updated_at = ?
          WHERE id = ? AND owner_uid = ? AND disabled = 0`,
      )
        .bind(JSON.stringify(legacyTools), endpoint, now, appId, context.uid)
        .run();
      if (Number(catalogUpdate.meta?.changes) !== 1)
        throw new McpAppOauthError(409, "app_catalog_changed");
    }
    const revision =
      observedDiscoveryRevision === null ? 0 : observedDiscoveryRevision + 1;
    const toolNames = tools.map((tool) => String(tool.name));
    return c.json(
      surface === "legacy"
        ? { tools_count: tools.length, tool_names: toolNames }
        : {
            app_id: appId,
            status: "ready",
            endpoint,
            transport: discovery.transport,
            protocol_version: protocolVersion,
            revision,
            tools_count: tools.length,
            tool_names: toolNames,
          },
      200,
      { "cache-control": "no-store" },
    );
  } catch (error) {
    const failureCode =
      error instanceof McpAppOauthError ? error.code : "discovery_unavailable";
    try {
      // Keep the last successful tools_json intact while marking this fetch
      // failed. A later successful discovery can atomically restore ready.
      if (observedDiscoveryRevision !== null) {
        await c.env.APP_DB.prepare(
          `UPDATE cf_mcp_app_discoveries
              SET status = 'failed', last_error = ?, updated_at = ?
            WHERE app_id = ? AND owner_uid = ? AND revision = ?`,
        )
          .bind(
            failureCode.slice(0, 2_000),
            now,
            appId,
            context.uid,
            observedDiscoveryRevision,
          )
          .run();
      }
    } catch {
      // Account-deletion fences intentionally reject this status update.
    }
    if (error instanceof McpAppOauthError && error.status === 401) {
      try {
        await c.env.APP_DB.prepare(
          `UPDATE cf_mcp_app_connections
              SET status = 'reauthorize', last_error = ?, updated_at = ?
            WHERE app_id = ? AND owner_uid = ? AND status = 'authorized' AND revision = ?`,
        )
          .bind(error.code, now, appId, context.uid, observedConnectionRevision)
          .run();
      } catch {
        // Account-deletion fences intentionally reject this status update.
      }
    }
    throw error;
  }
}

async function refresh(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: McpAppOauthDependencies,
  appIdOverride?: string,
  surface: McpAppOauthSurface = "namespaced",
): Promise<Response> {
  if (!surfaceEnabled(c.env, surface))
    throw new McpAppOauthError(404, "not_found");
  const body = appIdOverride ? null : parseBody(await requestText(c));
  const appId =
    appIdOverride ||
    (typeof body?.app_id === "string" ? body.app_id.trim() : "");
  if (!appId || appId.length > 256)
    throw new McpAppOauthError(422, "invalid_request");
  const connection = await c.env.APP_DB.prepare(
    `SELECT app_id, owner_uid, status, credential_envelope_enc, revision
       FROM cf_mcp_app_connections
      WHERE app_id = ? AND owner_uid = ?
      LIMIT 1`,
  )
    .bind(appId, context.uid)
    .first<{
      app_id?: string;
      owner_uid?: string;
      status?: string;
      credential_envelope_enc?: string | null;
      revision?: number;
    }>();
  if (!connection?.app_id || !connection.owner_uid)
    throw new McpAppOauthError(404, "app_not_found");
  if (connection.status === "reauthorize")
    throw new McpAppOauthError(401, "mcp_reauthorization_required");
  if (
    connection.status !== "authorized" ||
    typeof connection.credential_envelope_enc !== "string"
  )
    throw new McpAppOauthError(409, "mcp_authorization_required");
  const observedRevision = Number(connection.revision);
  if (!Number.isSafeInteger(observedRevision) || observedRevision < 0)
    throw new McpAppOauthError(409, "app_connection_changed");
  const now = nowSeconds(dependencies);
  if (!now) throw new McpAppOauthError(503, "clock_unavailable");

  let credentials: JsonObject;
  try {
    credentials = await decrypt(
      c.env,
      "connection",
      connection.owner_uid,
      connection.app_id,
      connection.credential_envelope_enc,
    );
  } catch (error) {
    if (error instanceof McpAppOauthError) throw error;
    throw new McpAppOauthError(401, "mcp_reauthorization_required");
  }
  const refreshToken = validatedCredential(
    credentials.refresh_token,
    "mcp_reauthorization_required",
    401,
    true,
  ) as string;
  const clientId = validatedCredential(
    credentials.client_id,
    "mcp_reauthorization_required",
    401,
    true,
  ) as string;
  const tokenEndpoint = validatedCredential(
    credentials.token_endpoint,
    "mcp_authorization_required",
    401,
    true,
  ) as string;
  if (!publicHttps(tokenEndpoint))
    throw new McpAppOauthError(401, "mcp_reauthorization_required");
  const clientSecret = validatedCredential(
    credentials.client_secret,
    "mcp_reauthorization_required",
    401,
  );
  const form = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: refreshToken,
    client_id: clientId,
    ...(clientSecret ? { client_secret: clientSecret } : {}),
  });
  let response: Response;
  try {
    response = await providerFetch(dependencies, tokenEndpoint, {
      method: "POST",
      headers: {
        "content-type": "application/x-www-form-urlencoded",
        accept: "application/json",
      },
      body: form.toString(),
    });
  } catch (error) {
    throw error;
  }
  if (response.status === 401) {
    const marked = await c.env.APP_DB.prepare(
      `UPDATE cf_mcp_app_connections
          SET status = 'reauthorize', credential_envelope_enc = NULL,
              revision = revision + 1, last_error = ?, updated_at = ?
        WHERE app_id = ? AND owner_uid = ? AND status = 'authorized' AND revision = ?`,
    )
      .bind(
        "mcp_reauthorization_required",
        now,
        connection.app_id,
        connection.owner_uid,
        observedRevision,
      )
      .run();
    if (Number(marked.meta?.changes) !== 1)
      throw new McpAppOauthError(409, "app_connection_changed");
    throw new McpAppOauthError(401, "mcp_reauthorization_required");
  }
  if (!response.ok) throw new McpAppOauthError(502, "token_refresh_failed");
  const payload = await boundedJson(response, "token_response_invalid");
  const accessToken = validatedCredential(
    payload.access_token,
    "token_response_invalid",
    502,
    true,
  ) as string;
  const nextRefreshToken =
    payload.refresh_token === undefined
      ? refreshToken
      : validatedCredential(
          payload.refresh_token,
          "token_response_invalid",
          502,
        );
  const expiresIn = Number(payload.expires_in || 3_600);
  if (!Number.isSafeInteger(expiresIn) || expiresIn < 1 || expiresIn > 86_400)
    throw new McpAppOauthError(502, "token_response_invalid");
  const nextEnvelope = await encrypt(
    c.env,
    "connection",
    connection.owner_uid,
    connection.app_id,
    {
      client_id: clientId,
      ...(clientSecret ? { client_secret: clientSecret } : {}),
      token_endpoint: tokenEndpoint,
      access_token: accessToken,
      refresh_token: nextRefreshToken,
      expires_in: expiresIn,
      issued_at: now,
    },
  );
  const updated = await c.env.APP_DB.prepare(
    `UPDATE cf_mcp_app_connections
        SET status = 'authorized', credential_envelope_enc = ?,
            revision = revision + 1, last_error = NULL, updated_at = ?
      WHERE app_id = ? AND owner_uid = ? AND status = 'authorized' AND revision = ?`,
  )
    .bind(
      nextEnvelope,
      now,
      connection.app_id,
      connection.owner_uid,
      observedRevision,
    )
    .run();
  if (Number(updated.meta?.changes) !== 1)
    throw new McpAppOauthError(409, "app_connection_changed");

  // Reuse the same bounded initialize/tools-list projection and its revision
  // CAS. A failed discovery leaves the newly refreshed credential authorized;
  // a 401 from the server transitions it to reauthorize in discover().
  return discover(c, context, dependencies, appId, surface);
}

async function callTool(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: McpAppOauthDependencies,
  appId: string,
): Promise<Response> {
  if (!surfaceEnabled(c.env, "namespaced"))
    throw new McpAppOauthError(404, "not_found");
  if (!appId || appId.length > 256)
    throw new McpAppOauthError(422, "invalid_request");
  const body = parseBody(await requestText(c));
  const bodyKeys = Object.keys(body);
  if (bodyKeys.some((key) => key !== "name" && key !== "arguments"))
    throw new McpAppOauthError(422, "invalid_request");
  const toolName = typeof body.name === "string" ? body.name.trim() : "";
  if (
    !toolName ||
    utf8Bytes(toolName) > MAX_TOOL_NAME_BYTES ||
    /[\u0000-\u001f\u007f]/.test(toolName)
  )
    throw new McpAppOauthError(422, "invalid_tool_name");
  const argsValue = body.arguments === undefined ? {} : body.arguments;
  if (!objectValue(argsValue))
    throw new McpAppOauthError(422, "invalid_tool_arguments");
  const args = argsValue as JsonObject;
  let argsJson: string;
  try {
    argsJson = JSON.stringify(args);
  } catch {
    throw new McpAppOauthError(422, "invalid_tool_arguments");
  }
  if (utf8Bytes(argsJson) > MAX_CALL_ARGUMENT_BYTES)
    throw new McpAppOauthError(413, "request_too_large");
  validateCallArguments(args);

  const row = await c.env.APP_DB.prepare(
    `SELECT c.app_id, c.owner_uid, c.status AS connection_status,
            c.credential_envelope_enc, c.revision AS connection_revision,
            d.endpoint, d.status AS discovery_status, d.tools_json
       FROM cf_user_enabled_apps u
       JOIN cf_mcp_app_connections c ON c.app_id = u.app_id AND c.owner_uid = u.uid
       JOIN cf_mcp_app_discoveries d ON d.app_id = c.app_id AND d.owner_uid = c.owner_uid
       JOIN cf_app_catalog a ON a.id = c.app_id
      WHERE u.uid = ? AND u.app_id = ?
        AND c.status = 'authorized' AND d.status = 'ready' AND a.disabled = 0
        AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents i WHERE i.uid = u.uid)
        AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones t WHERE t.uid = u.uid)
      LIMIT 1`,
  )
    .bind(context.uid, appId)
    .first<{
      app_id?: string;
      owner_uid?: string;
      connection_status?: string;
      credential_envelope_enc?: string | null;
      connection_revision?: number;
      endpoint?: string;
      discovery_status?: string;
      tools_json?: string;
    }>();
  if (
    !row?.app_id ||
    row.owner_uid !== context.uid ||
    row.connection_status !== "authorized" ||
    row.discovery_status !== "ready" ||
    typeof row.endpoint !== "string" ||
    !publicHttps(row.endpoint) ||
    typeof row.credential_envelope_enc !== "string"
  )
    throw new McpAppOauthError(404, "mcp_app_not_ready");
  const connectionRevision = Number(row.connection_revision);
  if (!Number.isSafeInteger(connectionRevision) || connectionRevision < 0)
    throw new McpAppOauthError(409, "app_connection_changed");
  if (typeof row.tools_json !== "string")
    throw new McpAppOauthError(409, "mcp_discovery_required");
  let tools: JsonObject[];
  try {
    tools = validateMcpTools(JSON.parse(row.tools_json));
  } catch (error) {
    if (error instanceof McpAppOauthError) throw error;
    throw new McpAppOauthError(409, "mcp_discovery_required");
  }
  if (!tools.some((tool) => tool.name === toolName))
    throw new McpAppOauthError(404, "tool_not_found");
  // Recheck the deletion fence and connection revision immediately before
  // decrypting/provider I/O. The call itself is read-only in D1, so a delete
  // cannot be resurrected by a late result; a 401 status update below is CASed.
  const fence = await c.env.APP_DB.prepare(
    `SELECT 1 AS ready FROM cf_mcp_app_connections c
      WHERE c.app_id = ? AND c.owner_uid = ? AND c.status = 'authorized' AND c.revision = ?
        AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents i WHERE i.uid = c.owner_uid)
        AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones t WHERE t.uid = c.owner_uid)
      LIMIT 1`,
  )
    .bind(appId, context.uid, connectionRevision)
    .first<{ ready?: number }>();
  if (!fence) throw new McpAppOauthError(409, "app_connection_changed");
  let credentials: JsonObject;
  try {
    credentials = await decrypt(
      c.env,
      "connection",
      context.uid,
      appId,
      row.credential_envelope_enc,
    );
  } catch {
    throw new McpAppOauthError(401, "mcp_reauthorization_required");
  }
  const accessToken = validatedCredential(
    credentials.access_token,
    "mcp_reauthorization_required",
    401,
  ) || "";
  let result: JsonObject | null;
  try {
    result = await callStreamableHttp(
      dependencies,
      row.endpoint,
      accessToken,
      toolName,
      args,
    );
    if (result === null)
      result = await callLegacySse(
        dependencies,
        row.endpoint,
        accessToken,
        toolName,
        args,
      );
  } catch (error) {
    if (error instanceof McpAppOauthError && error.status === 401) {
      const now = nowSeconds(dependencies);
      if (now) {
        try {
          await c.env.APP_DB.prepare(
            `UPDATE cf_mcp_app_connections
                SET status = 'reauthorize', credential_envelope_enc = NULL,
                    revision = revision + 1, last_error = ?, updated_at = ?
              WHERE app_id = ? AND owner_uid = ? AND status = 'authorized' AND revision = ?
                AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents i WHERE i.uid = owner_uid)
                AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones t WHERE t.uid = owner_uid)`,
          )
            .bind("mcp_reauthorization_required", now, appId, context.uid, connectionRevision)
            .run();
        } catch {
          // Deletion fences intentionally reject a late provider failure update.
        }
      }
    }
    throw error;
  }
  if (!result) throw new McpAppOauthError(502, "mcp_call_response_invalid");
  let responseBody: string;
  try {
    responseBody = JSON.stringify({
      app_id: appId,
      tool_name: toolName,
      result,
    });
  } catch {
    throw new McpAppOauthError(502, "mcp_call_response_invalid");
  }
  if (utf8Bytes(responseBody) > MAX_PROVIDER_RESPONSE_BYTES)
    throw new McpAppOauthError(502, "mcp_call_response_invalid");
  return c.json(
    { app_id: appId, tool_name: toolName, result },
    200,
    { "cache-control": "no-store" },
  );
}

async function install(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: McpAppOauthDependencies,
  appIdOverride?: string,
  surface: McpAppOauthSurface = "namespaced",
): Promise<Response> {
  if (!surfaceEnabled(c.env, surface))
    throw new McpAppOauthError(404, "not_found");
  const body = appIdOverride ? null : parseBody(await requestText(c));
  const appId =
    appIdOverride ||
    (typeof body?.app_id === "string" ? body.app_id.trim() : "");
  if (!appId || appId.length > 256)
    throw new McpAppOauthError(422, "invalid_request");
  const row = await c.env.APP_DB.prepare(
    `SELECT c.app_id, c.owner_uid, c.status AS connection_status,
            a.disabled, d.status AS discovery_status, d.revision AS discovery_revision,
            d.tools_json
       FROM cf_mcp_app_connections c
       JOIN cf_app_catalog a ON a.id = c.app_id
       LEFT JOIN cf_mcp_app_discoveries d ON d.app_id = c.app_id AND d.owner_uid = c.owner_uid
      WHERE c.app_id = ? AND c.owner_uid = ?
      LIMIT 1`,
  )
    .bind(appId, context.uid)
    .first<{
      app_id?: string;
      owner_uid?: string;
      connection_status?: string;
      disabled?: number;
      discovery_status?: string | null;
      discovery_revision?: number | null;
      tools_json?: string | null;
    }>();
  if (!row?.app_id || !row.owner_uid || Number(row.disabled) === 1)
    throw new McpAppOauthError(404, "app_not_found");
  if (row.connection_status !== "authorized")
    throw new McpAppOauthError(409, "mcp_authorization_required");
  if (row.discovery_status !== "ready" || typeof row.tools_json !== "string")
    throw new McpAppOauthError(409, "mcp_discovery_required");
  let tools: unknown;
  try {
    tools = JSON.parse(row.tools_json);
  } catch {
    throw new McpAppOauthError(503, "mcp_discovery_unavailable");
  }
  if (!Array.isArray(tools) || tools.length === 0 || tools.length > MAX_TOOLS)
    throw new McpAppOauthError(503, "mcp_discovery_unavailable");
  const now = nowSeconds(dependencies);
  if (!now) throw new McpAppOauthError(503, "clock_unavailable");
  const inserted = await c.env.APP_DB.prepare(
    "INSERT OR IGNORE INTO cf_user_enabled_apps (uid, app_id, created_at) VALUES (?, ?, ?)",
  )
    .bind(context.uid, appId, now)
    .run();
  if (Number(inserted.meta?.changes) === 1) {
    await c.env.APP_DB.prepare(
      "UPDATE cf_app_catalog SET installs = MAX(0, installs + 1), updated_at = ? WHERE id = ? AND (owner_uid IS NULL OR owner_uid != ?)",
    )
      .bind(now, appId, context.uid)
      .run();
  }
  return c.json(
    {
      app_id: appId,
      status: "installed",
      discovery_revision: Number(row.discovery_revision || 0),
      tools_count: tools.length,
    },
    200,
    { "cache-control": "no-store" },
  );
}

export function registerMcpAppOauthRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
  dependencies: McpAppOauthDependencies = {},
  surface: McpAppOauthSurface = "namespaced",
): void {
  if (surface === "legacy") {
    app.post("/v1/apps/mcp", async (c) => {
      const context = await requestContext(c);
      if (!context) return c.json({ error: "unauthorized" }, 401);
      try {
        return await startLegacy(c, context, dependencies);
      } catch (error) {
        return errorResponse(c, error);
      }
    });
    app.post("/v1/apps/:app_id/mcp/refresh", async (c) => {
      const context = await requestContext(c);
      if (!context) return c.json({ error: "unauthorized" }, 401);
      try {
        return await refresh(
          c,
          context,
          dependencies,
          c.req.param("app_id") || "",
          "legacy",
        );
      } catch (error) {
        return errorResponse(c, error);
      }
    });
    app.get("/v1/apps/mcp/callback", (c) =>
      callback(c, dependencies, "legacy"),
    );
    return;
  }
  app.post("/v2/cf/apps/mcp/authorize", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await start(c, context, dependencies);
    } catch (error) {
      return errorResponse(c, error);
    }
  });
  app.post("/v2/cf/apps/mcp/discover", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await discover(c, context, dependencies);
    } catch (error) {
      return errorResponse(c, error);
    }
  });
  app.post("/v2/cf/apps/mcp/refresh", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await refresh(c, context, dependencies);
    } catch (error) {
      return errorResponse(c, error);
    }
  });
  app.post("/v2/cf/apps/mcp/tools/:appId/call", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await callTool(
        c,
        context,
        dependencies,
        c.req.param("appId") || "",
      );
    } catch (error) {
      return errorResponse(c, error);
    }
  });
  app.post("/v2/cf/apps/mcp/install", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await install(c, context, dependencies);
    } catch (error) {
      return errorResponse(c, error);
    }
  });
  app.get("/v2/cf/apps/mcp/callback", (c) => callback(c, dependencies));
}
