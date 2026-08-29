import type { JobsEnv } from "./env";

const STRIPE_API_ORIGIN = "https://api.stripe.com";
const STRIPE_RESPONSE_MAX_BYTES = 64 * 1_024;
const STRIPE_TIMEOUT_MS = 15_000;

export class StripeConfigurationError extends Error {}

export class StripeResponseError extends Error {
  constructor(
    readonly status: number,
    readonly userMessage: string | null,
  ) {
    super("Stripe request failed");
  }
}

export function stripeSecretKey(env: JobsEnv): string {
  const key = env.STRIPE_SECRET_KEY?.trim();
  if (
    !key ||
    key.length > 512 ||
    key.includes(":") ||
    /[^\x21-\x7e]/.test(key)
  ) {
    throw new StripeConfigurationError("Stripe cleanup credential unavailable");
  }
  return key;
}

async function boundedStripeJson(response: Response): Promise<unknown> {
  const declared = Number(response.headers.get("content-length"));
  if (
    Number.isFinite(declared) &&
    (declared < 0 || declared > STRIPE_RESPONSE_MAX_BYTES)
  ) {
    response.body?.cancel();
    throw new Error("Stripe response is too large");
  }
  if (!response.body) throw new Error("Stripe response body is missing");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > STRIPE_RESPONSE_MAX_BYTES) {
        throw new Error("Stripe response is too large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    throw new Error("Stripe response is invalid");
  }
}

function stripeErrorMessage(body: unknown): string | null {
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;
  const error = (body as Record<string, unknown>).error;
  if (!error || typeof error !== "object" || Array.isArray(error)) return null;
  const message = (error as Record<string, unknown>).message;
  return typeof message === "string" && message.length <= 1_000
    ? message
    : null;
}

export async function stripeRequest(
  env: JobsEnv,
  path: string,
  options: {
    method?: "GET" | "POST" | "DELETE";
    form?: URLSearchParams;
    idempotencyKey?: string;
  } = {},
): Promise<unknown> {
  if (!path.startsWith("/v1/")) throw new Error("invalid Stripe API path");
  const headers = new Headers({
    accept: "application/json",
    authorization: `Basic ${btoa(`${stripeSecretKey(env)}:`)}`,
  });
  if (options.form) {
    headers.set("content-type", "application/x-www-form-urlencoded");
  }
  if (options.idempotencyKey) {
    headers.set("idempotency-key", options.idempotencyKey);
  }
  const response = await fetch(`${STRIPE_API_ORIGIN}${path}`, {
    method: options.method || "GET",
    headers,
    body: options.form?.toString(),
    signal: AbortSignal.timeout(STRIPE_TIMEOUT_MS),
  });
  const body = await boundedStripeJson(response);
  if (!response.ok) {
    throw new StripeResponseError(response.status, stripeErrorMessage(body));
  }
  return body;
}
