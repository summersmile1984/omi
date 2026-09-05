import { attachAuthContext } from "./auth";
import type { EdgeEnv } from "./env";
import type { AuthContext } from "../shared/auth-context";

type AccountCutoverControl = {
  state?: unknown;
  client_action?: unknown;
  product_traffic_allowed?: unknown;
  migration?: { destination_backend_bound?: unknown } | null;
};

export const ACCOUNT_CUTOVER_CONTROL_PATH = "/v1/account/cutover/control";

type AccountControlEnv = Pick<
  EdgeEnv,
  | "API_CORE"
  | "INTERNAL_ASSERTION_SECRET"
  | "ACCOUNT_ACTIVATION_FENCE_ENABLED"
  | "ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED"
>;

// Core owns initialization and deletion fences. Privacy requests also read this
// owner, but Jobs decides their eligibility while ordinary traffic is fenced.
export async function readCloudflareAccountControl(
  clientRequest: Request,
  env: AccountControlEnv,
  auth: AuthContext,
  requestId: string,
): Promise<AccountCutoverControl | Response> {
  const target = new URL(
    ACCOUNT_CUTOVER_CONTROL_PATH,
    "https://api-core.internal",
  );
  const headers = new Headers({ "x-request-id": requestId });
  for (const name of ["x-app-platform", "x-app-build", "x-app-version"]) {
    const value = clientRequest.headers.get(name);
    if (value) headers.set(name, value);
  }
  await attachAuthContext(
    headers,
    auth,
    env.INTERNAL_ASSERTION_SECRET,
    "api-core",
    { method: "GET", url: target },
  );
  if (!headers.has("x-omi-auth-context")) {
    return Response.json(
      { error: "account data plane unavailable", retryable: true },
      { status: 503, headers: { "cache-control": "no-store" } },
    );
  }

  let response: Response;
  try {
    response = await env.API_CORE.fetch(
      new Request(target, { method: "GET", headers }),
    );
  } catch {
    return Response.json(
      { error: "account data plane unavailable", retryable: true },
      { status: 503, headers: { "cache-control": "no-store" } },
    );
  }
  if (!response.ok) {
    return Response.json(
      { error: "account data plane unavailable", retryable: true },
      { status: 503, headers: { "cache-control": "no-store" } },
    );
  }

  let control: AccountCutoverControl;
  try {
    control = (await response.json()) as AccountCutoverControl;
    if (!control || typeof control !== "object" || Array.isArray(control))
      throw new Error("invalid account control");
  } catch {
    return Response.json(
      { error: "account data plane unavailable", retryable: true },
      { status: 503, headers: { "cache-control": "no-store" } },
    );
  }
  return control;
}

export async function cloudflareProductTrafficDenial(
  clientRequest: Request,
  env: AccountControlEnv,
  auth: AuthContext,
  requestId: string,
): Promise<Response | null> {
  if (
    env.ACCOUNT_ACTIVATION_FENCE_ENABLED !== "true" &&
    env.ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED !== "true"
  )
    return null;
  const control = await readCloudflareAccountControl(
    clientRequest,
    env,
    auth,
    requestId,
  );
  if (control instanceof Response) return control;
  const destinationBound =
    control.migration?.destination_backend_bound === true;
  if (
    control.state === "new" &&
    control.product_traffic_allowed === true &&
    destinationBound
  ) {
    return null;
  }

  return Response.json(
    {
      error: "account data plane not active",
      state: typeof control.state === "string" ? control.state : "unknown",
      client_action:
        typeof control.client_action === "string"
          ? control.client_action
          : "migration_maintenance",
      retryable: true,
    },
    { status: 409, headers: { "cache-control": "no-store" } },
  );
}
