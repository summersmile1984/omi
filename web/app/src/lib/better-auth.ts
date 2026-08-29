import type { WebAuthUser } from './auth-types';

let currentUser: WebAuthUser | null = null;
let restoreInFlight: Promise<WebAuthUser | null> | null = null;
const listeners = new Set<(user: WebAuthUser | null) => void>();

export const isBetterAuthEnabled = process.env.NEXT_PUBLIC_AUTH_MODE === 'better-auth';
export type BetterAuthSocialProvider = 'google' | 'apple';
export type BetterAuthOAuthClient = {
  clientId: string;
  name: string;
  uri: string | null;
  icon: string | null;
};

const MAX_OAUTH_QUERY_LENGTH = 16_384;

export function signedOAuthQuery(search: string): string | undefined {
  if (!search || search.length > MAX_OAUTH_QUERY_LENGTH) return undefined;
  const params = new URLSearchParams(search);
  if (params.getAll('sig').length !== 1) return undefined;
  const signedNames = params.getAll('ba_param');
  if (!signedNames.length || signedNames.length > 64) return undefined;
  const allowed = new Set(signedNames);
  const signed = new URLSearchParams();
  for (const [key, value] of params.entries()) {
    if (key === 'sig' || key === 'ba_param' || allowed.has(key)) {
      signed.append(key, value);
    }
  }
  return signed.toString() || undefined;
}

function browserOAuthQuery(): string | undefined {
  return typeof window === 'undefined'
    ? undefined
    : signedOAuthQuery(window.location.search);
}

export function oauthRedirectUrl(body: unknown, origin: string): string | null {
  if (
    !body ||
    typeof body !== 'object' ||
    (body as { redirect?: unknown }).redirect !== true ||
    typeof (body as { url?: unknown }).url !== 'string'
  ) {
    return null;
  }
  const raw = (body as { url: string }).url;
  if (!raw || raw.length > 4_096) return null;
  try {
    const target = new URL(raw, origin);
    const localHttp =
      target.protocol === 'http:' &&
      (target.hostname === 'localhost' ||
        target.hostname === '127.0.0.1' ||
        target.hostname === '[::1]');
    return target.protocol === 'https:' || localHttp ? target.href : null;
  } catch {
    return null;
  }
}

function publish(user: WebAuthUser | null): void {
  currentUser = user;
  listeners.forEach((listener) => listener(user));
}

function userFromResponse(value: unknown): WebAuthUser | null {
  const raw =
    value && typeof value === 'object'
      ? (value as { user?: Record<string, unknown> }).user
      : null;
  const uid = typeof raw?.id === 'string' ? raw.id : null;
  if (!uid) return null;
  const name = typeof raw?.name === 'string' ? raw.name : null;
  const email = typeof raw?.email === 'string' ? raw.email : null;
  const photoURL = typeof raw?.image === 'string' ? raw.image : null;
  return {
    uid,
    displayName: name,
    email,
    photoURL,
    // Better Auth browser sessions are intentionally cookie-only. API calls
    // use the same-origin proxy and never expose the long-lived session token
    // to JavaScript.
    getIdToken: async () => null,
  };
}

async function authRequest(
  path: string,
  init: RequestInit = {},
): Promise<{ response: Response; body: unknown }> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set('content-type', 'application/json');
  const response = await fetch(`/api/better-auth/${path}`, {
    ...init,
    headers,
    credentials: 'include',
  });
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Keep the status as the authoritative failure when the auth service did
    // not return JSON.
  }
  return { response, body };
}

function errorMessage(body: unknown, fallback: string): string {
  if (
    body &&
    typeof body === 'object' &&
    typeof (body as { message?: unknown }).message === 'string'
  ) {
    return String((body as { message: string }).message);
  }
  return fallback;
}

function completeAuth(body: unknown): WebAuthUser {
  const user = userFromResponse(body);
  if (!user) throw new Error('Better Auth returned an invalid user');
  publish(user);
  return user;
}

export async function signInWithEmail(
  email: string,
  password: string,
): Promise<WebAuthUser | null> {
  const oauthQuery = browserOAuthQuery();
  const { response, body } = await authRequest('sign-in/email', {
    method: 'POST',
    body: JSON.stringify({
      email,
      password,
      ...(oauthQuery ? { oauth_query: oauthQuery } : {}),
    }),
  });
  if (!response.ok) throw new Error(errorMessage(body, 'Unable to sign in'));
  const redirect = oauthRedirectUrl(
    body,
    typeof window === 'undefined' ? 'https://invalid.local' : window.location.origin,
  );
  if (redirect) {
    if (typeof window === 'undefined')
      throw new Error('OAuth redirect requires a browser');
    window.location.assign(redirect);
    return null;
  }
  return completeAuth(body);
}

export async function signUpWithEmail(
  name: string,
  email: string,
  password: string,
): Promise<WebAuthUser | null> {
  const oauthQuery = browserOAuthQuery();
  const { response, body } = await authRequest('sign-up/email', {
    method: 'POST',
    body: JSON.stringify({
      name,
      email,
      password,
      ...(oauthQuery ? { oauth_query: oauthQuery } : {}),
    }),
  });
  if (!response.ok) throw new Error(errorMessage(body, 'Unable to create account'));
  const redirect = oauthRedirectUrl(
    body,
    typeof window === 'undefined' ? 'https://invalid.local' : window.location.origin,
  );
  if (redirect) {
    if (typeof window === 'undefined')
      throw new Error('OAuth redirect requires a browser');
    window.location.assign(redirect);
    return null;
  }
  return completeAuth(body);
}

export async function getBetterAuthSocialProviders(): Promise<
  BetterAuthSocialProvider[]
> {
  const { response, body } = await authRequest('omi-capabilities');
  if (!response.ok || !body || typeof body !== 'object') return [];
  const providers = (body as { social_providers?: unknown }).social_providers;
  if (!Array.isArray(providers)) return [];
  return providers.filter(
    (provider): provider is BetterAuthSocialProvider =>
      provider === 'google' || provider === 'apple',
  );
}

export async function getBetterAuthSocialSignInUrl(
  provider: BetterAuthSocialProvider,
): Promise<string> {
  const oauthQuery = browserOAuthQuery();
  const { response, body } = await authRequest('sign-in/social', {
    method: 'POST',
    body: JSON.stringify({
      provider,
      callbackURL: '/conversations',
      errorCallbackURL: '/login',
      ...(oauthQuery ? { oauth_query: oauthQuery } : {}),
    }),
  });
  if (!response.ok) {
    throw new Error(errorMessage(body, `Unable to sign in with ${provider}`));
  }
  const url =
    body &&
    typeof body === 'object' &&
    typeof (body as { url?: unknown }).url === 'string'
      ? (body as { url: string }).url
      : null;
  if (!url) throw new Error('Better Auth returned an invalid OAuth redirect');
  const target = new URL(url);
  const expectedHost =
    provider === 'google' ? 'accounts.google.com' : 'appleid.apple.com';
  if (target.protocol !== 'https:' || target.hostname !== expectedHost) {
    throw new Error('Better Auth returned an untrusted OAuth redirect');
  }
  return url;
}

export async function getBetterAuthOAuthClient(
  clientId: string,
  oauthQuery: string,
): Promise<BetterAuthOAuthClient> {
  if (!clientId || clientId.length > 2_048 || !oauthQuery) {
    throw new Error('Invalid OAuth authorization request');
  }
  const { response, body } = await authRequest('oauth2/public-client-prelogin', {
    method: 'POST',
    body: JSON.stringify({ client_id: clientId, oauth_query: oauthQuery }),
  });
  if (!response.ok || !body || typeof body !== 'object') {
    throw new Error(errorMessage(body, 'Unable to load OAuth client'));
  }
  const value = body as Record<string, unknown>;
  if (value.client_id !== clientId)
    throw new Error('Better Auth returned an invalid OAuth client');
  return {
    clientId,
    name:
      typeof value.client_name === 'string' && value.client_name.trim()
        ? value.client_name.trim().slice(0, 200)
        : 'MCP client',
    uri: typeof value.client_uri === 'string' ? value.client_uri : null,
    icon: typeof value.logo_uri === 'string' ? value.logo_uri : null,
  };
}

export async function submitBetterAuthOAuthConsent(
  accept: boolean,
  oauthQuery: string,
): Promise<string> {
  if (!oauthQuery) throw new Error('Invalid OAuth authorization request');
  const { response, body } = await authRequest('oauth2/consent', {
    method: 'POST',
    body: JSON.stringify({ accept, oauth_query: oauthQuery }),
  });
  if (!response.ok) throw new Error(errorMessage(body, 'Unable to save OAuth consent'));
  const redirect = oauthRedirectUrl(
    body,
    typeof window === 'undefined' ? 'https://invalid.local' : window.location.origin,
  );
  if (!redirect) throw new Error('Better Auth returned an invalid OAuth redirect');
  return redirect;
}

export async function signOutBetterAuth(): Promise<void> {
  const { response } = await authRequest('sign-out', {
    method: 'POST',
    body: JSON.stringify({}),
  });
  publish(null);
  if (!response.ok && response.status !== 401) throw new Error('Unable to sign out');
}

async function restoreUser(): Promise<WebAuthUser | null> {
  const { response, body } = await authRequest('get-session');
  if (!response.ok) return null;
  return userFromResponse(body);
}

function restoreCurrentUser(): Promise<WebAuthUser | null> {
  if (!restoreInFlight) {
    restoreInFlight = restoreUser().finally(() => {
      restoreInFlight = null;
    });
  }
  return restoreInFlight;
}

export function onBetterAuthStateChange(
  callback: (user: WebAuthUser | null) => void,
): () => void {
  listeners.add(callback);
  if (currentUser) {
    callback(currentUser);
  } else {
    void restoreCurrentUser()
      .then(publish)
      .catch(() => publish(null));
  }
  return () => listeners.delete(callback);
}

export function getBetterAuthUser(): WebAuthUser | null {
  return currentUser;
}
