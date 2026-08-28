import type { WebAuthUser } from './auth-types';

let currentUser: WebAuthUser | null = null;
let restoreInFlight: Promise<WebAuthUser | null> | null = null;
const listeners = new Set<(user: WebAuthUser | null) => void>();

export const isBetterAuthEnabled = process.env.NEXT_PUBLIC_AUTH_MODE === 'better-auth';

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
): Promise<WebAuthUser> {
  const { response, body } = await authRequest('sign-in/email', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) throw new Error(errorMessage(body, 'Unable to sign in'));
  return completeAuth(body);
}

export async function signUpWithEmail(
  name: string,
  email: string,
  password: string,
): Promise<WebAuthUser> {
  const { response, body } = await authRequest('sign-up/email', {
    method: 'POST',
    body: JSON.stringify({ name, email, password }),
  });
  if (!response.ok) throw new Error(errorMessage(body, 'Unable to create account'));
  return completeAuth(body);
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
