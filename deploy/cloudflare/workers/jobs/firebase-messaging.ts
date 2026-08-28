import { recordFallback } from "../shared/fallback";
import type { JobsEnv } from "./env";

const GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";
const FIREBASE_SCOPE = "https://www.googleapis.com/auth/firebase.messaging";
const OUTBOX_BATCH_SIZE = 10;
const MAX_TOKENS_PER_USER = 20;
const DELIVERY_LEASE_SECONDS = 10 * 60;
const MAX_DELIVERY_ATTEMPTS = 10;
const MAX_CREDENTIAL_BYTES = 32_000;

type ServiceAccount = {
  projectId: string;
  clientEmail: string;
  privateKey: string;
};

type OutboxRow = {
  notification_id: string;
  uid: string;
  title: string;
  body: string;
  data_json: string;
  attempts: number;
};

type DeliveryOptions = {
  fetcher?: typeof fetch;
  accessToken?: (
    serviceAccount: ServiceAccount,
    now: number,
    fetcher: typeof fetch,
  ) => Promise<string>;
};

let cachedAccessToken: {
  value: string;
  expiresAt: number;
  email: string;
} | null = null;

function base64Url(value: string | Uint8Array): string {
  const bytes =
    typeof value === "string" ? new TextEncoder().encode(value) : value;
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/, "");
}

function parseServiceAccount(value: string | undefined): ServiceAccount | null {
  if (
    !value ||
    new TextEncoder().encode(value).byteLength > MAX_CREDENTIAL_BYTES
  )
    return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
    return null;
  const object = parsed as Record<string, unknown>;
  const projectId =
    typeof object.project_id === "string" ? object.project_id : "";
  const clientEmail =
    typeof object.client_email === "string" ? object.client_email : "";
  const privateKey =
    typeof object.private_key === "string" ? object.private_key : "";
  if (
    !/^[a-z0-9][a-z0-9-]{2,62}$/.test(projectId) ||
    !/^[^\s@]+@[^\s@]+$/.test(clientEmail) ||
    !privateKey.includes("BEGIN PRIVATE KEY")
  ) {
    return null;
  }
  return { projectId, clientEmail, privateKey };
}

function privateKeyBytes(pem: string): Uint8Array<ArrayBuffer> {
  const base64 = pem
    .replace("-----BEGIN PRIVATE KEY-----", "")
    .replace("-----END PRIVATE KEY-----", "")
    .replace(/\s+/g, "");
  const decoded = atob(base64);
  const bytes = new Uint8Array(new ArrayBuffer(decoded.length));
  for (let index = 0; index < decoded.length; index += 1) {
    bytes[index] = decoded.charCodeAt(index);
  }
  return bytes;
}

async function firebaseAccessToken(
  account: ServiceAccount,
  now: number,
  fetcher: typeof fetch,
): Promise<string> {
  if (
    cachedAccessToken &&
    cachedAccessToken.email === account.clientEmail &&
    cachedAccessToken.expiresAt > now + 60
  ) {
    return cachedAccessToken.value;
  }
  const header = base64Url(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const claims = base64Url(
    JSON.stringify({
      iss: account.clientEmail,
      scope: FIREBASE_SCOPE,
      aud: GOOGLE_TOKEN_URL,
      iat: now,
      exp: now + 3_600,
    }),
  );
  const unsigned = `${header}.${claims}`;
  const key = await crypto.subtle.importKey(
    "pkcs8",
    privateKeyBytes(account.privateKey),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    new TextEncoder().encode(unsigned),
  );
  const assertion = `${unsigned}.${base64Url(new Uint8Array(signature))}`;
  const response = await fetcher(GOOGLE_TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion,
    }),
  });
  if (!response.ok) throw new Error("firebase token exchange unavailable");
  const payload = (await response.json()) as Record<string, unknown>;
  if (typeof payload.access_token !== "string" || !payload.access_token) {
    throw new Error("firebase token exchange returned invalid response");
  }
  const expiresIn = Math.max(
    60,
    Math.min(3_600, Number(payload.expires_in) || 3_600),
  );
  cachedAccessToken = {
    value: payload.access_token,
    expiresAt: now + expiresIn,
    email: account.clientEmail,
  };
  return payload.access_token;
}

function notificationData(value: string): Record<string, string> {
  if (new TextEncoder().encode(value).byteLength > 16_000) return {};
  try {
    const parsed = JSON.parse(value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
      return {};
    return Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>)
        .filter(([, item]) => typeof item === "string")
        .slice(0, 20) as Array<[string, string]>,
    );
  } catch {
    return {};
  }
}

export async function sendFirebaseNotification(
  account: ServiceAccount,
  accessToken: string,
  token: string,
  notification: Pick<OutboxRow, "title" | "body" | "data_json">,
  fetcher: typeof fetch = fetch,
): Promise<"sent" | "invalid_token" | "retry"> {
  const response = await fetcher(
    `https://fcm.googleapis.com/v1/projects/${account.projectId}/messages:send`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${accessToken}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        message: {
          token,
          notification: { title: notification.title, body: notification.body },
          data: notificationData(notification.data_json),
          android: { priority: "high" },
          apns: { headers: { "apns-priority": "10" } },
        },
      }),
    },
  );
  if (response.ok) return "sent";
  let responseText = "";
  try {
    responseText = (await response.text()).slice(0, 8_000);
  } catch {
    responseText = "";
  }
  if (response.status === 404 || responseText.includes("UNREGISTERED")) {
    return "invalid_token";
  }
  return "retry";
}

async function recordDeliveryFailure(
  env: JobsEnv,
  row: OutboxRow,
  now: number,
  error: string,
): Promise<void> {
  const attempts = Number(row.attempts || 0) + 1;
  const terminal = attempts >= MAX_DELIVERY_ATTEMPTS;
  const delay = Math.min(6 * 60 * 60, 30 * 2 ** Math.min(attempts, 10));
  await env.APP_DB.prepare(
    "UPDATE cf_fair_use_notification_outbox SET status = ?, not_before = ?, lease_until = NULL, " +
      "last_error = ?, updated_at = ? WHERE notification_id = ? AND status = 'sending'",
  )
    .bind(
      terminal ? "failed" : "pending",
      now + delay,
      error,
      now,
      row.notification_id,
    )
    .run();
}

export async function drainFairUseNotifications(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1000),
  options: DeliveryOptions = {},
): Promise<number> {
  const result = await env.APP_DB.prepare(
    "SELECT notification_id, uid, title, body, data_json, attempts " +
      "FROM cf_fair_use_notification_outbox WHERE " +
      "(status = 'pending' AND not_before <= ?) OR (status = 'sending' AND lease_until <= ?) " +
      "ORDER BY created_at ASC LIMIT ?",
  )
    .bind(now, now, OUTBOX_BATCH_SIZE)
    .all<OutboxRow>();
  const rows = result.results || [];
  if (!rows.length) return 0;
  const account = parseServiceAccount(env.FIREBASE_SERVICE_ACCOUNT_JSON);
  const fetcher = options.fetcher || fetch;
  const accessTokenProvider = options.accessToken || firebaseAccessToken;
  let accessToken: string | null = null;

  let delivered = 0;
  for (const row of rows) {
    const claimed = await env.APP_DB.prepare(
      "UPDATE cf_fair_use_notification_outbox SET status = 'sending', attempts = attempts + 1, " +
        "lease_until = ?, updated_at = ? WHERE notification_id = ? AND " +
        "((status = 'pending' AND not_before <= ?) OR (status = 'sending' AND lease_until <= ?))",
    )
      .bind(now + DELIVERY_LEASE_SECONDS, now, row.notification_id, now, now)
      .run();
    if (claimed.meta?.changes !== 1) continue;
    const tokens = await env.APP_DB.prepare(
      "SELECT token FROM cf_user_fcm_tokens WHERE uid = ? ORDER BY updated_at DESC LIMIT ?",
    )
      .bind(row.uid, MAX_TOKENS_PER_USER)
      .all<{ token: string }>();
    const validTokens = (tokens.results || []).filter(
      (tokenRow) => typeof tokenRow.token === "string" && !!tokenRow.token,
    );
    if (validTokens.length && !account) {
      recordFallback({
        component: "other",
        from: "fcm",
        to: "notification_outbox",
        reason: "dependency_unavailable",
        outcome: "degraded",
      });
      await recordDeliveryFailure(
        env,
        row,
        now,
        "firebase authentication unavailable",
      );
      continue;
    }
    if (validTokens.length && !accessToken && account) {
      try {
        accessToken = await accessTokenProvider(account, now, fetcher);
      } catch {
        await recordDeliveryFailure(
          env,
          row,
          now,
          "firebase authentication unavailable",
        );
        continue;
      }
    }
    let retry = false;
    for (const tokenRow of validTokens) {
      let outcome: "sent" | "invalid_token" | "retry";
      try {
        outcome = await sendFirebaseNotification(
          account!,
          accessToken!,
          tokenRow.token,
          row,
          fetcher,
        );
      } catch {
        outcome = "retry";
      }
      if (outcome === "invalid_token") {
        await env.APP_DB.prepare(
          "DELETE FROM cf_user_fcm_tokens WHERE uid = ? AND token = ?",
        )
          .bind(row.uid, tokenRow.token)
          .run();
      } else if (outcome === "retry") {
        retry = true;
      }
    }
    if (retry) {
      await recordDeliveryFailure(
        env,
        row,
        now,
        "firebase delivery unavailable",
      );
      continue;
    }
    await env.APP_DB.prepare(
      "UPDATE cf_fair_use_notification_outbox SET status = 'sent', lease_until = NULL, " +
        "last_error = NULL, updated_at = ? WHERE notification_id = ? AND status = 'sending'",
    )
      .bind(now, row.notification_id)
      .run();
    delivered += 1;
  }
  return delivered;
}
