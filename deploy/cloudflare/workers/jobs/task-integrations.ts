import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobsEnv } from "./env";

const PROVIDERS = ["todoist", "asana", "google_tasks", "clickup"] as const;
const INTEGRATIONS = ["apple_reminders", ...PROVIDERS] as const;
const PROVIDER_SET = new Set<string>(PROVIDERS);
const INTEGRATION_SET = new Set<string>(INTEGRATIONS);
const CONFIGURATION_FIELDS = new Set([
  "user_gid",
  "workspace_gid",
  "workspace_name",
  "project_gid",
  "project_name",
  "default_list_id",
  "default_list_title",
  "user_id",
  "team_id",
  "team_name",
  "space_id",
  "space_name",
  "list_id",
  "list_name",
]);
const TOKEN_SENTINEL = "configured";
const OAUTH_STATE_TTL_SECONDS = 10 * 60;
const TOKEN_REFRESH_BUFFER_SECONDS = 5 * 60;
const MAX_PROVIDER_RESPONSE_BYTES = 1_000_000;
const MAX_TITLE_LENGTH = 500;
const MAX_DESCRIPTION_LENGTH = 10_000;
const MAX_CONFIGURATION_VALUE_LENGTH = 2_000;

type Provider = (typeof PROVIDERS)[number];
type Integration = (typeof INTEGRATIONS)[number];
type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;
type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type TaskIntegrationDependencies = Readonly<{
  fetchImpl?: FetchLike;
  now?: () => number;
}>;

type IntegrationRow = {
  uid: string;
  app_key: Integration;
  connected: number;
  access_token_enc: string | null;
  refresh_token_enc: string | null;
  token_expires_at: number | null;
  configuration_json: string;
  created_at: number;
  updated_at: number;
};

type Credentials = {
  row: IntegrationRow;
  accessToken: string;
  refreshToken: string | null;
};

type ProviderConfiguration = {
  clientId: string;
  clientSecret: string;
  redirectUri: string;
};

class TaskIntegrationError extends Error {
  constructor(
    readonly status: 400 | 401 | 404 | 409 | 413 | 422 | 502 | 503,
    readonly detail: string,
  ) {
    super(detail);
  }
}

function nowSeconds(dependencies?: TaskIntegrationDependencies) {
  return dependencies?.now?.() ?? Math.floor(Date.now() / 1_000);
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function providerValue(value: string): Provider | null {
  return PROVIDER_SET.has(value) ? (value as Provider) : null;
}

function integrationValue(value: string): Integration | null {
  return INTEGRATION_SET.has(value) ? (value as Integration) : null;
}

function base64Url(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function decodeBase64Url(value: string) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function randomToken(bytes: number) {
  return base64Url(crypto.getRandomValues(new Uint8Array(bytes)));
}

async function sha256Bytes(value: string) {
  return new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
  );
}

async function sha256Hex(value: string) {
  return Array.from(await sha256Bytes(value), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function encryptionSecret(env: JobsEnv) {
  const value = env.TASK_INTEGRATION_TOKEN_ENCRYPTION_SECRET;
  if (
    typeof value !== "string" ||
    new TextEncoder().encode(value).byteLength < 32
  ) {
    throw new TaskIntegrationError(
      503,
      "Task integration encryption is not configured",
    );
  }
  return value;
}

async function credentialKey(env: JobsEnv) {
  return crypto.subtle.importKey(
    "raw",
    await sha256Bytes(encryptionSecret(env)),
    { name: "AES-GCM" },
    false,
    ["encrypt", "decrypt"],
  );
}

function credentialContext(uid: string, appKey: Provider, field: string) {
  return new TextEncoder().encode(
    `omi:task-integration:v1\0${uid}\0${appKey}\0${field}`,
  );
}

async function encryptCredential(
  env: JobsEnv,
  uid: string,
  appKey: Provider,
  field: string,
  value: string,
) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    {
      name: "AES-GCM",
      iv,
      additionalData: credentialContext(uid, appKey, field),
    },
    await credentialKey(env),
    new TextEncoder().encode(value),
  );
  return `v1.${base64Url(iv)}.${base64Url(new Uint8Array(ciphertext))}`;
}

async function decryptCredential(
  env: JobsEnv,
  uid: string,
  appKey: Provider,
  field: string,
  envelope: string,
) {
  const parts = envelope.split(".");
  if (parts.length !== 3 || parts[0] !== "v1") {
    throw new Error("invalid task integration credential envelope");
  }
  const plaintext = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: decodeBase64Url(parts[1]),
      additionalData: credentialContext(uid, appKey, field),
    },
    await credentialKey(env),
    decodeBase64Url(parts[2]),
  );
  const value = new TextDecoder().decode(plaintext);
  if (!value) throw new Error("empty task integration credential");
  return value;
}

function publicApiBase(env: JobsEnv) {
  const value = env.PUBLIC_API_BASE_URL?.trim();
  if (!value) {
    throw new TaskIntegrationError(
      503,
      "Public API base URL is not configured",
    );
  }
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol !== "https:" ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash
    ) {
      throw new Error("invalid base URL");
    }
    parsed.pathname = parsed.pathname.replace(/\/+$/, "") + "/";
    return parsed;
  } catch {
    throw new TaskIntegrationError(
      503,
      "Public API base URL is not configured",
    );
  }
}

function callbackPath(provider: Provider) {
  return `/v2/integrations/${provider === "google_tasks" ? "google-tasks" : provider}/callback`;
}

function providerConfiguration(
  env: JobsEnv,
  provider: Provider,
): ProviderConfiguration {
  const values = {
    todoist: [env.TODOIST_CLIENT_ID, env.TODOIST_CLIENT_SECRET],
    asana: [env.ASANA_CLIENT_ID, env.ASANA_CLIENT_SECRET],
    google_tasks: [env.GOOGLE_TASKS_CLIENT_ID, env.GOOGLE_TASKS_CLIENT_SECRET],
    clickup: [env.CLICKUP_CLIENT_ID, env.CLICKUP_CLIENT_SECRET],
  }[provider];
  const clientId = values[0]?.trim();
  const clientSecret = values[1]?.trim();
  if (!clientId || !clientSecret) {
    throw new TaskIntegrationError(503, `${provider} is not configured`);
  }
  encryptionSecret(env);
  return {
    clientId,
    clientSecret,
    redirectUri: new URL(callbackPath(provider), publicApiBase(env)).toString(),
  };
}

function configurationFromRow(row: IntegrationRow) {
  try {
    const parsed = objectValue(JSON.parse(row.configuration_json));
    if (!parsed) throw new Error("invalid configuration");
    return Object.fromEntries(
      Object.entries(parsed).filter(
        ([key, value]) =>
          CONFIGURATION_FIELDS.has(key) && typeof value === "string",
      ),
    ) as Record<string, string>;
  } catch {
    throw new TaskIntegrationError(503, "Task integration data is invalid");
  }
}

function publicIntegration(row: IntegrationRow) {
  const integration: Record<string, unknown> = {
    connected: row.connected === 1,
    ...configurationFromRow(row),
  };
  if (row.access_token_enc) integration.access_token = TOKEN_SENTINEL;
  if (row.token_expires_at) {
    integration.expires_at = new Date(
      row.token_expires_at * 1_000,
    ).toISOString();
  }
  return integration;
}

async function readIntegration(env: JobsEnv, uid: string, appKey: Integration) {
  return env.APP_DB.prepare(
    "SELECT uid, app_key, connected, access_token_enc, refresh_token_enc, token_expires_at, configuration_json, created_at, updated_at " +
      "FROM cf_task_integrations WHERE uid = ? AND app_key = ?",
  )
    .bind(uid, appKey)
    .first<IntegrationRow>();
}

function normalizeConfigurationPatch(payload: Record<string, unknown>) {
  const patch = new Map<string, string | null>();
  for (const [key, value] of Object.entries(payload)) {
    if (!CONFIGURATION_FIELDS.has(key)) continue;
    if (value === null) {
      patch.set(key, null);
      continue;
    }
    if (
      typeof value !== "string" ||
      value.length === 0 ||
      value.length > MAX_CONFIGURATION_VALUE_LENGTH
    ) {
      throw new TaskIntegrationError(422, `Invalid ${key}`);
    }
    patch.set(key, value);
  }
  return patch;
}

function mergeConfiguration(
  current: Record<string, string>,
  patch: Map<string, string | null>,
) {
  const merged = { ...current };
  for (const [key, value] of patch) {
    if (value === null) delete merged[key];
    else merged[key] = value;
  }
  const encoded = JSON.stringify(merged);
  if (encoded.length > 16_000) {
    throw new TaskIntegrationError(
      413,
      "Task integration configuration is too large",
    );
  }
  return encoded;
}

function optionalToken(payload: Record<string, unknown>, field: string) {
  const value = payload[field];
  if (value === undefined || value === TOKEN_SENTINEL) return undefined;
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 16_000
  ) {
    throw new TaskIntegrationError(422, `Invalid ${field}`);
  }
  return value;
}

async function jsonPayload(c: JobsContext) {
  const contentLength = Number(c.req.header("content-length") || "0");
  if (Number.isFinite(contentLength) && contentLength > 32_000) {
    throw new TaskIntegrationError(413, "Request body is too large");
  }
  try {
    const value = objectValue(await c.req.json());
    if (!value) throw new Error("not an object");
    return value;
  } catch (error) {
    if (error instanceof TaskIntegrationError) throw error;
    throw new TaskIntegrationError(400, "Invalid JSON body");
  }
}

async function saveIntegration(
  env: JobsEnv,
  uid: string,
  appKey: Integration,
  payload: Record<string, unknown>,
  now: number,
) {
  const current = await readIntegration(env, uid, appKey);
  const provider = providerValue(appKey);
  const connectedValue = payload.connected;
  if (connectedValue !== undefined && typeof connectedValue !== "boolean") {
    throw new TaskIntegrationError(422, "Invalid connected value");
  }
  const connected = connectedValue === undefined ? true : connectedValue;
  const accessToken = optionalToken(payload, "access_token");
  const refreshToken = optionalToken(payload, "refresh_token");
  let accessTokenEnc = current?.access_token_enc ?? null;
  let refreshTokenEnc = current?.refresh_token_enc ?? null;
  if (provider && typeof accessToken === "string") {
    accessTokenEnc = await encryptCredential(
      env,
      uid,
      provider,
      "access_token",
      accessToken,
    );
  } else if (accessToken === null) {
    accessTokenEnc = null;
  }
  if (provider && typeof refreshToken === "string") {
    refreshTokenEnc = await encryptCredential(
      env,
      uid,
      provider,
      "refresh_token",
      refreshToken,
    );
  } else if (refreshToken === null) {
    refreshTokenEnc = null;
  }
  if (provider && connected && !accessTokenEnc) {
    throw new TaskIntegrationError(422, "Access token is required");
  }
  const currentConfiguration = current ? configurationFromRow(current) : {};
  const configurationJson = mergeConfiguration(
    currentConfiguration,
    normalizeConfigurationPatch(payload),
  );
  await env.APP_DB.prepare(
    "INSERT INTO cf_task_integrations " +
      "(uid, app_key, connected, access_token_enc, refresh_token_enc, token_expires_at, configuration_json, created_at, updated_at) " +
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) " +
      "ON CONFLICT(uid, app_key) DO UPDATE SET connected = excluded.connected, access_token_enc = excluded.access_token_enc, " +
      "refresh_token_enc = excluded.refresh_token_enc, token_expires_at = excluded.token_expires_at, " +
      "configuration_json = excluded.configuration_json, updated_at = excluded.updated_at",
  )
    .bind(
      uid,
      appKey,
      connected ? 1 : 0,
      accessTokenEnc,
      refreshTokenEnc,
      current?.token_expires_at ?? null,
      configurationJson,
      current?.created_at ?? now,
      now,
    )
    .run();
}

async function providerJson(response: Response) {
  const declaredLength = Number(response.headers.get("content-length") || "0");
  if (
    Number.isFinite(declaredLength) &&
    declaredLength > MAX_PROVIDER_RESPONSE_BYTES
  ) {
    throw new TaskIntegrationError(502, "Provider response is too large");
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.byteLength > MAX_PROVIDER_RESPONSE_BYTES) {
    throw new TaskIntegrationError(502, "Provider response is too large");
  }
  if (bytes.byteLength === 0) return {};
  try {
    const value = JSON.parse(new TextDecoder().decode(bytes));
    return value;
  } catch {
    throw new TaskIntegrationError(502, "Provider returned invalid JSON");
  }
}

async function providerFetch(
  dependencies: TaskIntegrationDependencies | undefined,
  input: string | URL,
  init: RequestInit,
) {
  try {
    return await (dependencies?.fetchImpl ?? fetch)(input, init);
  } catch {
    throw new TaskIntegrationError(502, "Task provider is unavailable");
  }
}

function formBody(values: Record<string, string>) {
  return new URLSearchParams(values).toString();
}

async function refreshCredentials(
  env: JobsEnv,
  credentials: Credentials,
  dependencies?: TaskIntegrationDependencies,
) {
  const provider = providerValue(credentials.row.app_key);
  if (
    !provider ||
    !credentials.refreshToken ||
    (provider !== "asana" && provider !== "google_tasks")
  ) {
    throw new TaskIntegrationError(
      401,
      `${credentials.row.app_key} authentication expired`,
    );
  }
  const config = providerConfiguration(env, provider);
  const endpoint =
    provider === "asana"
      ? "https://app.asana.com/-/oauth_token"
      : "https://oauth2.googleapis.com/token";
  const response = await providerFetch(dependencies, endpoint, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: formBody({
      grant_type: "refresh_token",
      client_id: config.clientId,
      client_secret: config.clientSecret,
      refresh_token: credentials.refreshToken,
    }),
  });
  const tokenData = objectValue(await providerJson(response));
  const newAccessToken = tokenData?.access_token;
  if (!response.ok || typeof newAccessToken !== "string" || !newAccessToken) {
    if (response.status === 400 || response.status === 401) {
      await env.APP_DB.prepare(
        "UPDATE cf_task_integrations SET connected = 0, updated_at = ? WHERE uid = ? AND app_key = ?",
      )
        .bind(nowSeconds(dependencies), credentials.row.uid, provider)
        .run();
    }
    throw new TaskIntegrationError(401, `${provider} authentication expired`);
  }
  const newRefreshToken =
    typeof tokenData.refresh_token === "string" && tokenData.refresh_token
      ? tokenData.refresh_token
      : credentials.refreshToken;
  const expiresIn = Number(tokenData.expires_in);
  const expiresAt =
    Number.isFinite(expiresIn) && expiresIn > 0
      ? nowSeconds(dependencies) + Math.floor(expiresIn)
      : null;
  const accessTokenEnc = await encryptCredential(
    env,
    credentials.row.uid,
    provider,
    "access_token",
    newAccessToken,
  );
  const refreshTokenEnc = await encryptCredential(
    env,
    credentials.row.uid,
    provider,
    "refresh_token",
    newRefreshToken,
  );
  await env.APP_DB.prepare(
    "UPDATE cf_task_integrations SET connected = 1, access_token_enc = ?, refresh_token_enc = ?, token_expires_at = ?, updated_at = ? " +
      "WHERE uid = ? AND app_key = ?",
  )
    .bind(
      accessTokenEnc,
      refreshTokenEnc,
      expiresAt,
      nowSeconds(dependencies),
      credentials.row.uid,
      provider,
    )
    .run();
  return {
    row: {
      ...credentials.row,
      connected: 1,
      access_token_enc: accessTokenEnc,
      refresh_token_enc: refreshTokenEnc,
      token_expires_at: expiresAt,
    },
    accessToken: newAccessToken,
    refreshToken: newRefreshToken,
  } satisfies Credentials;
}

async function integrationCredentials(
  env: JobsEnv,
  uid: string,
  provider: Provider,
  dependencies?: TaskIntegrationDependencies,
) {
  const row = await readIntegration(env, uid, provider);
  if (!row) throw new TaskIntegrationError(404, `Not connected to ${provider}`);
  if (!row.connected || !row.access_token_enc) {
    throw new TaskIntegrationError(401, `${provider} is not authenticated`);
  }
  let credentials: Credentials;
  try {
    credentials = {
      row,
      accessToken: await decryptCredential(
        env,
        uid,
        provider,
        "access_token",
        row.access_token_enc,
      ),
      refreshToken: row.refresh_token_enc
        ? await decryptCredential(
            env,
            uid,
            provider,
            "refresh_token",
            row.refresh_token_enc,
          )
        : null,
    };
  } catch (error) {
    if (error instanceof TaskIntegrationError) throw error;
    throw new TaskIntegrationError(
      503,
      "Task integration credentials are unavailable",
    );
  }
  if (
    credentials.refreshToken &&
    (provider === "asana" || provider === "google_tasks") &&
    (row.token_expires_at === null ||
      row.token_expires_at <=
        nowSeconds(dependencies) + TOKEN_REFRESH_BUFFER_SECONDS)
  ) {
    credentials = await refreshCredentials(env, credentials, dependencies);
  }
  return credentials;
}

async function requestWithCredentials(
  env: JobsEnv,
  uid: string,
  provider: Provider,
  makeRequest: (accessToken: string) => Promise<Response>,
  dependencies?: TaskIntegrationDependencies,
) {
  let credentials = await integrationCredentials(
    env,
    uid,
    provider,
    dependencies,
  );
  let response = await makeRequest(credentials.accessToken);
  if (response.status !== 401) return response;
  await response.arrayBuffer();
  if (
    credentials.refreshToken &&
    (provider === "asana" || provider === "google_tasks")
  ) {
    credentials = await refreshCredentials(env, credentials, dependencies);
    response = await makeRequest(credentials.accessToken);
    if (response.status !== 401) return response;
    await response.arrayBuffer();
  }
  await env.APP_DB.prepare(
    "UPDATE cf_task_integrations SET connected = 0, updated_at = ? WHERE uid = ? AND app_key = ?",
  )
    .bind(nowSeconds(dependencies), uid, provider)
    .run();
  throw new TaskIntegrationError(401, `${provider} authentication expired`);
}

function authorizationHeader(token: string) {
  return `Bearer ${token}`;
}

async function authenticatedProviderJson(
  env: JobsEnv,
  uid: string,
  provider: Provider,
  url: string,
  init: RequestInit,
  dependencies?: TaskIntegrationDependencies,
) {
  const response = await requestWithCredentials(
    env,
    uid,
    provider,
    (token) => {
      const headers = new Headers(init.headers);
      headers.set("authorization", authorizationHeader(token));
      return providerFetch(dependencies, url, { ...init, headers });
    },
    dependencies,
  );
  const payload = await providerJson(response);
  if (!response.ok) {
    throw new TaskIntegrationError(
      response.status >= 500 ? 502 : response.status === 404 ? 404 : 400,
      `${provider} request failed`,
    );
  }
  return payload;
}

function oauthAuthorizationUrl(
  provider: Provider,
  configuration: ProviderConfiguration,
  state: string,
) {
  let url: URL;
  if (provider === "todoist") {
    url = new URL("https://app.todoist.com/oauth/authorize");
    url.searchParams.set("scope", "data:read_write");
    url.searchParams.set("response_type", "code");
  } else if (provider === "asana") {
    url = new URL("https://app.asana.com/-/oauth_authorize");
    url.searchParams.set("response_type", "code");
    url.searchParams.set(
      "scope",
      "tasks:read tasks:write workspaces:read projects:read users:read",
    );
  } else if (provider === "google_tasks") {
    url = new URL("https://accounts.google.com/o/oauth2/v2/auth");
    url.searchParams.set("response_type", "code");
    url.searchParams.set("scope", "https://www.googleapis.com/auth/tasks");
    url.searchParams.set("access_type", "offline");
    url.searchParams.set("prompt", "consent");
  } else {
    url = new URL("https://app.clickup.com/api");
  }
  url.searchParams.set("client_id", configuration.clientId);
  url.searchParams.set("redirect_uri", configuration.redirectUri);
  url.searchParams.set("state", state);
  return url.toString();
}

async function exchangeAuthorizationCode(
  env: JobsEnv,
  provider: Provider,
  code: string,
  dependencies?: TaskIntegrationDependencies,
) {
  const configuration = providerConfiguration(env, provider);
  let endpoint: string;
  let body: string;
  let contentType: string;
  if (provider === "todoist") {
    endpoint = "https://api.todoist.com/oauth/access_token";
    contentType = "application/x-www-form-urlencoded";
    body = formBody({
      client_id: configuration.clientId,
      client_secret: configuration.clientSecret,
      code,
    });
  } else if (provider === "asana") {
    endpoint = "https://app.asana.com/-/oauth_token";
    contentType = "application/x-www-form-urlencoded";
    body = formBody({
      grant_type: "authorization_code",
      client_id: configuration.clientId,
      client_secret: configuration.clientSecret,
      redirect_uri: configuration.redirectUri,
      code,
    });
  } else if (provider === "google_tasks") {
    endpoint = "https://oauth2.googleapis.com/token";
    contentType = "application/x-www-form-urlencoded";
    body = formBody({
      grant_type: "authorization_code",
      client_id: configuration.clientId,
      client_secret: configuration.clientSecret,
      redirect_uri: configuration.redirectUri,
      code,
    });
  } else {
    endpoint = "https://api.clickup.com/api/v2/oauth/token";
    contentType = "application/json";
    body = JSON.stringify({
      client_id: configuration.clientId,
      client_secret: configuration.clientSecret,
      code,
    });
  }
  const response = await providerFetch(dependencies, endpoint, {
    method: "POST",
    headers: { "content-type": contentType },
    body,
  });
  const tokenData = objectValue(await providerJson(response));
  if (
    !response.ok ||
    !tokenData ||
    typeof tokenData.access_token !== "string"
  ) {
    throw new TaskIntegrationError(400, "Token exchange failed");
  }
  return tokenData;
}

async function oauthAdditionalConfiguration(
  provider: Provider,
  accessToken: string,
  dependencies?: TaskIntegrationDependencies,
) {
  try {
    if (provider === "asana") {
      const response = await providerFetch(
        dependencies,
        "https://app.asana.com/api/1.0/users/me",
        { headers: { authorization: `Bearer ${accessToken}` } },
      );
      const payload = objectValue(await providerJson(response));
      const data = objectValue(payload?.data);
      return response.ok && typeof data?.gid === "string"
        ? { user_gid: data.gid }
        : {};
    }
    if (provider === "google_tasks") {
      const response = await providerFetch(
        dependencies,
        "https://tasks.googleapis.com/tasks/v1/users/@me/lists",
        { headers: { authorization: `Bearer ${accessToken}` } },
      );
      const payload = objectValue(await providerJson(response));
      const first = Array.isArray(payload?.items)
        ? objectValue(payload.items[0])
        : null;
      return response.ok && typeof first?.id === "string"
        ? {
            default_list_id: first.id,
            ...(typeof first.title === "string"
              ? { default_list_title: first.title }
              : {}),
          }
        : {};
    }
  } catch {
    // Provider metadata is optional. The encrypted token still establishes the
    // connection and the user can complete provider-specific setup later.
  }
  return {};
}

async function storeOAuthConnection(
  env: JobsEnv,
  uid: string,
  provider: Provider,
  tokenData: Record<string, unknown>,
  dependencies?: TaskIntegrationDependencies,
) {
  const accessToken = tokenData.access_token as string;
  const refreshToken =
    typeof tokenData.refresh_token === "string" && tokenData.refresh_token
      ? tokenData.refresh_token
      : null;
  const expiresIn = Number(tokenData.expires_in);
  const expiresAt =
    Number.isFinite(expiresIn) && expiresIn > 0
      ? nowSeconds(dependencies) + Math.floor(expiresIn)
      : null;
  const current = await readIntegration(env, uid, provider);
  const configuration = {
    ...(current ? configurationFromRow(current) : {}),
    ...(await oauthAdditionalConfiguration(
      provider,
      accessToken,
      dependencies,
    )),
  };
  await env.APP_DB.prepare(
    "INSERT INTO cf_task_integrations " +
      "(uid, app_key, connected, access_token_enc, refresh_token_enc, token_expires_at, configuration_json, created_at, updated_at) " +
      "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?) " +
      "ON CONFLICT(uid, app_key) DO UPDATE SET connected = 1, access_token_enc = excluded.access_token_enc, " +
      "refresh_token_enc = excluded.refresh_token_enc, token_expires_at = excluded.token_expires_at, " +
      "configuration_json = excluded.configuration_json, updated_at = excluded.updated_at",
  )
    .bind(
      uid,
      provider,
      await encryptCredential(env, uid, provider, "access_token", accessToken),
      refreshToken
        ? await encryptCredential(
            env,
            uid,
            provider,
            "refresh_token",
            refreshToken,
          )
        : null,
      expiresAt,
      JSON.stringify(configuration),
      current?.created_at ?? nowSeconds(dependencies),
      nowSeconds(dependencies),
    )
    .run();
}

function deepLink(provider: Provider, success: boolean, error?: string) {
  const host = provider === "google_tasks" ? "google-tasks" : provider;
  const url = new URL(`omi://${host}/callback`);
  if (success) {
    url.searchParams.set("success", "true");
    if (provider === "asana" || provider === "clickup") {
      url.searchParams.set("requires_setup", "true");
    }
  } else {
    url.searchParams.set("error", error || "server_error");
  }
  return url.toString();
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function oauthHtml(provider: Provider, success: boolean, error?: string) {
  const destination = deepLink(provider, success, error);
  const title = success ? "Authentication successful" : "Authentication error";
  const detail = success
    ? "Returning to Omi…"
    : error === "invalid_state"
      ? "This authentication request is invalid or expired."
      : error === "missing_code"
        ? "No authorization code was received."
        : error === "config_error"
          ? "This integration is not configured."
          : "The provider could not complete authentication.";
  const redirectScript = success
    ? `<script>setTimeout(function(){location.replace(${JSON.stringify(destination)})},400)</script>`
    : "";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title><style>body{margin:0;background:#0a0a0a;color:#f5f5f5;font:16px system-ui;display:grid;min-height:100vh;place-items:center}.card{max-width:420px;padding:32px;border:1px solid #303030;border-radius:18px;background:#171717;text-align:center}h1{font-size:22px;margin:0 0 12px}p{color:#b8b8b8;margin:0 0 24px}a{color:#fff}</style></head><body><main class="card"><h1>${title}</h1><p>${detail}</p><a href="${escapeHtml(destination)}">Return to Omi</a></main>${redirectScript}</body></html>`;
}

function oauthResponse(provider: Provider, success: boolean, error?: string) {
  return new Response(oauthHtml(provider, success, error), {
    status: 200,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "content-security-policy":
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
      "x-content-type-options": "nosniff",
    },
  });
}

async function oauthCallback(
  c: JobsContext,
  provider: Provider,
  dependencies?: TaskIntegrationDependencies,
) {
  const code = c.req.query("code");
  const state = c.req.query("state");
  if (!code || !state || code.length > 8_000 || state.length > 256) {
    return oauthResponse(provider, false, "missing_code");
  }
  try {
    providerConfiguration(c.env, provider);
  } catch {
    return oauthResponse(provider, false, "config_error");
  }
  const stateRow = await c.env.APP_DB.prepare(
    "DELETE FROM cf_task_integration_oauth_states " +
      "WHERE state_hash = ? RETURNING uid, app_key, expires_at",
  )
    .bind(await sha256Hex(state))
    .first<{ uid: string; app_key: string; expires_at: number }>();
  if (
    !stateRow ||
    stateRow.app_key !== provider ||
    stateRow.expires_at <= nowSeconds(dependencies)
  ) {
    return oauthResponse(provider, false, "invalid_state");
  }
  try {
    const tokenData = await exchangeAuthorizationCode(
      c.env,
      provider,
      code,
      dependencies,
    );
    await storeOAuthConnection(
      c.env,
      stateRow.uid,
      provider,
      tokenData,
      dependencies,
    );
    return oauthResponse(provider, true);
  } catch {
    return oauthResponse(provider, false, "token_exchange_failed");
  }
}

export async function cleanupExpiredTaskIntegrationOAuthStates(
  env: Pick<JobsEnv, "APP_DB">,
  now: number,
) {
  await env.APP_DB.prepare(
    "DELETE FROM cf_task_integration_oauth_states WHERE expires_at <= ?",
  )
    .bind(now)
    .run();
}

function taskPayload(payload: Record<string, unknown>) {
  const title = typeof payload.title === "string" ? payload.title.trim() : "";
  if (!title || title.length > MAX_TITLE_LENGTH) {
    throw new TaskIntegrationError(422, "Invalid task title");
  }
  const description = payload.description;
  if (
    description !== undefined &&
    description !== null &&
    (typeof description !== "string" ||
      description.length > MAX_DESCRIPTION_LENGTH)
  ) {
    throw new TaskIntegrationError(422, "Invalid task description");
  }
  const dueDate = payload.due_date;
  let dueTimestamp: number | null = null;
  if (dueDate !== undefined && dueDate !== null) {
    if (typeof dueDate !== "string") {
      throw new TaskIntegrationError(
        400,
        "Invalid due_date; expected ISO 8601",
      );
    }
    dueTimestamp = Date.parse(dueDate);
    if (!Number.isFinite(dueTimestamp)) {
      throw new TaskIntegrationError(
        400,
        "Invalid due_date; expected ISO 8601",
      );
    }
  }
  return {
    title,
    description: typeof description === "string" ? description : null,
    dueTimestamp,
  };
}

function dateOnly(timestamp: number) {
  return new Date(timestamp).toISOString().slice(0, 10);
}

async function createProviderTask(
  env: JobsEnv,
  uid: string,
  provider: Provider,
  payload: Record<string, unknown>,
  dependencies?: TaskIntegrationDependencies,
) {
  const task = taskPayload(payload);
  const row = await readIntegration(env, uid, provider);
  if (!row) throw new TaskIntegrationError(404, `Not connected to ${provider}`);
  const configuration = configurationFromRow(row);
  let url: string;
  let body: Record<string, unknown>;
  if (provider === "todoist") {
    url = "https://api.todoist.com/api/v1/tasks";
    body = { content: task.title, priority: 2 };
    if (task.description) body.description = task.description;
    if (task.dueTimestamp !== null)
      body.due_string = dateOnly(task.dueTimestamp);
  } else if (provider === "asana") {
    if (!configuration.workspace_gid) {
      return { success: false, error: "No workspace configured" };
    }
    const data: Record<string, unknown> = {
      name: task.title,
      workspace: configuration.workspace_gid,
    };
    if (task.description) data.notes = task.description;
    if (task.dueTimestamp !== null) data.due_on = dateOnly(task.dueTimestamp);
    if (configuration.user_gid) data.assignee = configuration.user_gid;
    if (configuration.project_gid) data.projects = [configuration.project_gid];
    url = "https://app.asana.com/api/1.0/tasks";
    body = { data };
  } else if (provider === "google_tasks") {
    if (!configuration.default_list_id) {
      return { success: false, error: "No task list configured" };
    }
    url = `https://tasks.googleapis.com/tasks/v1/lists/${encodeURIComponent(configuration.default_list_id)}/tasks`;
    body = { title: task.title };
    if (task.description) body.notes = task.description;
    if (task.dueTimestamp !== null) {
      body.due = `${dateOnly(task.dueTimestamp)}T00:00:00.000Z`;
    }
  } else {
    if (!configuration.list_id) {
      return { success: false, error: "No list configured" };
    }
    url = `https://api.clickup.com/api/v2/list/${encodeURIComponent(configuration.list_id)}/task`;
    body = { name: task.title };
    if (task.description) body.description = task.description;
    if (task.dueTimestamp !== null) body.due_date = task.dueTimestamp;
  }
  try {
    const response = await requestWithCredentials(
      env,
      uid,
      provider,
      (token) =>
        providerFetch(dependencies, url, {
          method: "POST",
          headers: {
            authorization: authorizationHeader(token),
            "content-type": "application/json",
          },
          body: JSON.stringify(body),
        }),
      dependencies,
    );
    const result = objectValue(await providerJson(response));
    if (!response.ok) {
      return {
        success: false,
        error: `${provider} API error: ${response.status}`,
      };
    }
    const externalId =
      provider === "asana" ? objectValue(result?.data)?.gid : result?.id;
    return {
      success: true,
      external_task_id:
        typeof externalId === "string" || typeof externalId === "number"
          ? String(externalId)
          : null,
      error: null,
    };
  } catch (error) {
    if (error instanceof TaskIntegrationError && error.status === 401) {
      throw error;
    }
    return { success: false, error: "Task provider is unavailable" };
  }
}

function errorResponse(c: JobsContext, error: unknown) {
  if (error instanceof TaskIntegrationError) {
    return c.json({ detail: error.detail }, error.status);
  }
  return c.json({ error: "task_integrations_unavailable" }, 503);
}

function validProviderIdentifier(value: string) {
  if (!value || value.length > 100 || value.includes("/")) {
    throw new TaskIntegrationError(400, "Invalid provider identifier");
  }
  return encodeURIComponent(value);
}

export function registerTaskIntegrationRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
  dependencies?: TaskIntegrationDependencies,
) {
  for (const provider of PROVIDERS) {
    app.get(callbackPath(provider), (c) =>
      oauthCallback(c, provider, dependencies),
    );
  }

  app.get("/v1/task-integrations", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const [integrations, defaultRow] = await Promise.all([
        c.env.APP_DB.prepare(
          "SELECT uid, app_key, connected, access_token_enc, refresh_token_enc, token_expires_at, configuration_json, created_at, updated_at " +
            "FROM cf_task_integrations WHERE uid = ? ORDER BY app_key",
        )
          .bind(context.uid)
          .all<IntegrationRow>(),
        c.env.APP_DB.prepare(
          "SELECT default_app FROM cf_task_integration_defaults WHERE uid = ?",
        )
          .bind(context.uid)
          .first<{ default_app: string | null }>(),
      ]);
      return c.json({
        integrations: Object.fromEntries(
          integrations.results.map((row) => [
            row.app_key,
            publicIntegration(row),
          ]),
        ),
        default_app: defaultRow?.default_app || null,
      });
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.get("/v1/task-integrations/default", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const row = await c.env.APP_DB.prepare(
        "SELECT default_app FROM cf_task_integration_defaults WHERE uid = ?",
      )
        .bind(context.uid)
        .first<{ default_app: string | null }>();
      return c.json({ default_app: row?.default_app || null });
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.put("/v1/task-integrations/default", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const payload = await jsonPayload(c);
      const rawAppKey = payload.app_key;
      const appKey =
        rawAppKey === "" || rawAppKey === null
          ? null
          : typeof rawAppKey === "string"
            ? integrationValue(rawAppKey)
            : null;
      if (rawAppKey !== "" && rawAppKey !== null && !appKey) {
        throw new TaskIntegrationError(400, "Unsupported task integration");
      }
      await c.env.APP_DB.prepare(
        "INSERT INTO cf_task_integration_defaults (uid, default_app, updated_at) VALUES (?, ?, ?) " +
          "ON CONFLICT(uid) DO UPDATE SET default_app = excluded.default_app, updated_at = excluded.updated_at",
      )
        .bind(context.uid, appKey, nowSeconds(dependencies))
        .run();
      return c.json({ default_app: appKey });
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.get("/v1/task-integrations/asana/workspaces", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const payload = objectValue(
        await authenticatedProviderJson(
          c.env,
          context.uid,
          "asana",
          "https://app.asana.com/api/1.0/workspaces",
          { method: "GET" },
          dependencies,
        ),
      );
      return c.json({
        workspaces: Array.isArray(payload?.data) ? payload.data : [],
      });
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.get("/v1/task-integrations/asana/projects/:workspaceGid", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const workspaceGid = validProviderIdentifier(c.req.param("workspaceGid"));
      const payload = objectValue(
        await authenticatedProviderJson(
          c.env,
          context.uid,
          "asana",
          `https://app.asana.com/api/1.0/workspaces/${workspaceGid}/projects?archived=false&opt_fields=name%2Cgid%2Cowner`,
          { method: "GET" },
          dependencies,
        ),
      );
      return c.json({
        projects: Array.isArray(payload?.data) ? payload.data : [],
      });
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.get("/v1/task-integrations/clickup/teams", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const payload = objectValue(
        await authenticatedProviderJson(
          c.env,
          context.uid,
          "clickup",
          "https://api.clickup.com/api/v2/team",
          { method: "GET" },
          dependencies,
        ),
      );
      return c.json({
        teams: Array.isArray(payload?.teams) ? payload.teams : [],
      });
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.get("/v1/task-integrations/clickup/spaces/:teamId", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const teamId = validProviderIdentifier(c.req.param("teamId"));
      const payload = objectValue(
        await authenticatedProviderJson(
          c.env,
          context.uid,
          "clickup",
          `https://api.clickup.com/api/v2/team/${teamId}/space?archived=false`,
          { method: "GET" },
          dependencies,
        ),
      );
      return c.json({
        spaces: Array.isArray(payload?.spaces) ? payload.spaces : [],
      });
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.get("/v1/task-integrations/clickup/lists/:spaceId", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const spaceId = validProviderIdentifier(c.req.param("spaceId"));
      const payload = objectValue(
        await authenticatedProviderJson(
          c.env,
          context.uid,
          "clickup",
          `https://api.clickup.com/api/v2/space/${spaceId}/list?archived=false`,
          { method: "GET" },
          dependencies,
        ),
      );
      return c.json({
        lists: Array.isArray(payload?.lists) ? payload.lists : [],
      });
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.get("/v1/task-integrations/:appKey/oauth-url", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const provider = providerValue(c.req.param("appKey"));
      if (!provider) {
        throw new TaskIntegrationError(400, "Unsupported task integration");
      }
      const configuration = providerConfiguration(c.env, provider);
      const state = randomToken(32);
      const now = nowSeconds(dependencies);
      await c.env.APP_DB.batch([
        c.env.APP_DB.prepare(
          "DELETE FROM cf_task_integration_oauth_states WHERE expires_at <= ?",
        ).bind(now),
        c.env.APP_DB.prepare(
          "INSERT INTO cf_task_integration_oauth_states (state_hash, uid, app_key, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
        ).bind(
          await sha256Hex(state),
          context.uid,
          provider,
          now + OAUTH_STATE_TTL_SECONDS,
          now,
        ),
      ]);
      return c.json({
        auth_url: oauthAuthorizationUrl(provider, configuration, state),
      });
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.post("/v1/task-integrations/:appKey/tasks", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const provider = providerValue(c.req.param("appKey"));
      if (!provider) {
        throw new TaskIntegrationError(400, "Unsupported task integration");
      }
      return c.json(
        await createProviderTask(
          c.env,
          context.uid,
          provider,
          await jsonPayload(c),
          dependencies,
        ),
      );
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.put("/v1/task-integrations/:appKey", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const appKey = integrationValue(c.req.param("appKey"));
      if (!appKey) {
        throw new TaskIntegrationError(400, "Unsupported task integration");
      }
      await saveIntegration(
        c.env,
        context.uid,
        appKey,
        await jsonPayload(c),
        nowSeconds(dependencies),
      );
      return c.json({ status: "ok", app_key: appKey });
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.delete("/v1/task-integrations/:appKey", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const appKey = integrationValue(c.req.param("appKey"));
      if (!appKey) {
        throw new TaskIntegrationError(400, "Unsupported task integration");
      }
      const results = await c.env.APP_DB.batch([
        c.env.APP_DB.prepare(
          "DELETE FROM cf_task_integrations WHERE uid = ? AND app_key = ?",
        ).bind(context.uid, appKey),
        c.env.APP_DB.prepare(
          "UPDATE cf_task_integration_defaults SET default_app = NULL, updated_at = ? WHERE uid = ? AND default_app = ?",
        ).bind(nowSeconds(dependencies), context.uid, appKey),
      ]);
      if (results[0].meta?.changes !== 1) {
        throw new TaskIntegrationError(404, "Task integration not found");
      }
      return new Response(null, { status: 204 });
    } catch (error) {
      return errorResponse(c, error);
    }
  });
}
