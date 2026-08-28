import type { WebAuthUser } from './auth-types';

const TOKEN_STORAGE_KEY = 'omi.better-auth.bearer-token';
const isBrowser = typeof window !== 'undefined';

let currentToken: string | null = null;
let currentUser: WebAuthUser | null = null;
const listeners = new Set<(user: WebAuthUser | null) => void>();

export const isBetterAuthEnabled = process.env.NEXT_PUBLIC_AUTH_MODE === 'better-auth';

function storage(): Storage | null {
  if (!isBrowser) return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function readToken(): string | null {
  if (currentToken) return currentToken;
  const value = storage()?.getItem(TOKEN_STORAGE_KEY) || null;
  currentToken = value;
  return value;
}

function writeToken(token: string | null): void {
  currentToken = token;
  const target = storage();
  if (!target) return;
  if (token) target.setItem(TOKEN_STORAGE_KEY, token);
  else target.removeItem(TOKEN_STORAGE_KEY);
}

function publish(user: WebAuthUser | null): void {
  currentUser = user;
  listeners.forEach((listener) => listener(user));
}

function decodePayload(token: string): Record<string, unknown> | null {
  try {
    const encoded = token.split('.')[1];
    if (!encoded) return null;
    const normalized = encoded.replace(/-/g, '+').replace(/_/g, '/');
    const binary = atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '='));
    return JSON.parse(binary) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function userFromResponse(value: unknown, token: string): WebAuthUser | null {
  const raw =
    value && typeof value === 'object'
      ? (value as { user?: Record<string, unknown> }).user
      : null;
  const payload = decodePayload(token);
  const uid =
    typeof raw?.id === 'string'
      ? raw.id
      : typeof payload?.uid === 'string'
        ? payload.uid
        : payload?.sub;
  if (typeof uid !== 'string' || !uid) return null;
  const name = typeof raw?.name === 'string' ? raw.name : null;
  const email = typeof raw?.email === 'string' ? raw.email : null;
  const photoURL = typeof raw?.image === 'string' ? raw.image : null;
  return {
    uid,
    displayName: name,
    email,
    photoURL,
    getIdToken: async () => readToken(),
  };
}

async function authRequest(
  path: string,
  init: RequestInit = {},
): Promise<{ response: Response; body: unknown }> {
  const headers = new Headers(init.headers);
  headers.set('content-type', 'application/json');
  const token = readToken();
  if (token) headers.set('authorization', `Bearer ${token}`);
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

async function completeAuth(body: unknown): Promise<WebAuthUser> {
  const token =
    body &&
    typeof body === 'object' &&
    typeof (body as { token?: unknown }).token === 'string'
      ? String((body as { token: string }).token)
      : null;
  if (!token) throw new Error('Better Auth did not return a bearer token');
  writeToken(token);
  const user = userFromResponse(body, token);
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
  writeToken(null);
  publish(null);
  if (!response.ok && response.status !== 401) throw new Error('Unable to sign out');
}

async function restoreUser(token: string): Promise<WebAuthUser | null> {
  const { response, body } = await authRequest('get-session', {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!response.ok) return null;
  return userFromResponse(body, token);
}

export function onBetterAuthStateChange(
  callback: (user: WebAuthUser | null) => void,
): () => void {
  listeners.add(callback);
  const token = readToken();
  if (!token) {
    publish(null);
  } else if (currentUser) {
    callback(currentUser);
  } else {
    void restoreUser(token)
      .then((user) => {
        if (!user) writeToken(null);
        publish(user);
      })
      .catch(() => {
        writeToken(null);
        publish(null);
      });
  }
  return () => listeners.delete(callback);
}

export function getBetterAuthToken(): Promise<string | null> {
  return Promise.resolve(readToken());
}

export function getBetterAuthUser(): WebAuthUser | null {
  return currentUser;
}
