import { webProfile, type WebProfile } from './web-profile';

export interface AppUser {
  uid: string;
  displayName: string | null;
  email: string | null;
  photoURL: string | null;
}

export class AuthRequestError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
  }
}

export type AuthFetch = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

interface SessionDependencies {
  fetch: AuthFetch;
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
  now: () => number;
}

function userFromResponse(value: unknown): AppUser | null {
  if (!value || typeof value !== 'object') return null;
  const user = value as Record<string, unknown>;
  if (typeof user.id !== 'string' || !user.id) return null;
  return {
    uid: user.id,
    displayName: typeof user.name === 'string' ? user.name : null,
    email: typeof user.email === 'string' ? user.email : null,
    photoURL: typeof user.image === 'string' ? user.image : null,
  };
}

/** Opaque Better Auth sessions persist per tab/profile; access JWTs stay in memory. */
export class AuthSession {
  private session: string | null = null;
  private user: AppUser | null = null;
  private jwt: { token: string; expires: number } | null = null;
  private restoring: Promise<void> | null = null;
  private refreshing: { session: string; promise: Promise<string | null> } | null = null;
  private listeners = new Set<(user: AppUser | null) => void>();
  readonly storageKey: string;

  constructor(readonly profile: WebProfile, private dependencies: SessionDependencies) {
    this.storageKey = `auth-session:${profile.name}:${profile.auth_base_url}`;
  }

  get currentUser() {
    return this.user;
  }

  subscribe(listener: (user: AppUser | null) => void): () => void {
    this.listeners.add(listener);
    listener(this.user);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private publish(user: AppUser | null) {
    this.user = user;
    for (const listener of this.listeners) listener(user);
  }

  private clear() {
    this.dependencies.storage.removeItem(this.storageKey);
    this.session = null;
    this.jwt = null;
    this.publish(null);
  }

  private async request(
    path: string,
    body?: unknown,
    session?: string,
  ): Promise<Response> {
    let response: Response;
    try {
      response = await this.dependencies.fetch(
        `${this.profile.auth_base_url.replace(/\/$/, '')}/api/auth/${path}`,
        {
          method: body === undefined ? 'GET' : 'POST',
          // The explicit bearer credential works across origins without relying
          // on third-party cookies. OAuth is a separate, capability-gated flow.
          credentials: 'omit',
          cache: 'no-store',
          headers: {
            ...(body === undefined ? {} : { 'content-type': 'application/json' }),
            ...(session ? { authorization: `Bearer ${session}` } : {}),
          },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        },
      );
    } catch {
      throw new AuthRequestError(
        503,
        'Sign-in is temporarily unavailable. Please try again.',
      );
    }
    if (!response.ok) {
      if (response.status === 401 && session && session === this.session) this.clear();
      throw new AuthRequestError(
        response.status,
        response.status === 401
          ? session
            ? 'Your session has expired. Please sign in again.'
            : 'Check your email and password and try again.'
          : response.status >= 500
          ? 'Sign-in is temporarily unavailable. Please try again.'
          : 'Unable to sign in. Check your details and try again.',
      );
    }
    return response;
  }

  async restore(): Promise<void> {
    if (!this.restoring) {
      this.restoring = this.restoreStored().catch((error) => {
        this.restoring = null;
        throw error;
      });
    }
    return this.restoring;
  }

  private async restoreStored() {
    const saved = this.dependencies.storage.getItem(this.storageKey);
    if (!saved) return;
    this.session = saved;
    try {
      const response = await this.request('get-session', undefined, saved);
      const body = (await response.json()) as { user?: unknown } | null;
      if (this.session !== saved) return;
      const user = userFromResponse(body?.user);
      if (!user) this.clear();
      else this.publish(user);
    } catch (error) {
      if (error instanceof AuthRequestError && error.status === 401) return;
      throw error;
    }
  }

  async signIn(email: string, password: string): Promise<void> {
    return this.authenticate('sign-in/email', { email, password });
  }

  async signUp(name: string, email: string, password: string): Promise<void> {
    return this.authenticate('sign-up/email', { name, email, password });
  }

  private async authenticate(path: string, credentials: object) {
    const response = await this.request(path, credentials);
    const body = (await response.json()) as { user?: unknown };
    const user = userFromResponse(body.user);
    const session = response.headers.get('set-auth-token');
    if (!user || !session)
      throw new AuthRequestError(
        502,
        'The sign-in response is incomplete. Please try again.',
      );
    this.dependencies.storage.setItem(this.storageKey, session);
    this.session = session;
    this.jwt = null;
    this.restoring = Promise.resolve();
    this.publish(user);
  }

  async signOut(): Promise<void> {
    await this.restore();
    const session = this.session;
    if (session) {
      try {
        await this.request('sign-out', {}, session);
      } catch (error) {
        if (!(error instanceof AuthRequestError) || error.status !== 401) throw error;
      }
    }
    // A delayed logout cannot erase a newer sign-in.
    if (!this.session || this.session === session) this.clear();
  }

  async getToken(forceRefresh = false): Promise<string | null> {
    await this.restore();
    if (!this.session || !this.user) return null;
    if (!forceRefresh && this.jwt && this.jwt.expires - this.dependencies.now() > 60_000)
      return this.jwt.token;
    if (this.refreshing?.session !== this.session) {
      const session = this.session;
      const user = this.user;
      const refresh = { session, promise: Promise.resolve<string | null>(null) };
      refresh.promise = this.exchange(session, user).finally(() => {
        if (this.refreshing === refresh) this.refreshing = null;
      });
      this.refreshing = refresh;
    }
    return this.refreshing.promise;
  }

  private async exchange(session: string, user: AppUser): Promise<string | null> {
    const response = await this.request('token', undefined, session);
    const body = (await response.json()) as { token?: unknown };
    if (this.session !== session) return null;
    if (typeof body.token !== 'string')
      throw new AuthRequestError(502, 'The access token response is incomplete.');
    // This is a cache lifetime check, not signature verification. The trusted
    // auth origin issues the token; each backend verifies its signature/session.
    let claims: Record<string, unknown>;
    try {
      const encoded = body.token.split('.')[1];
      claims = JSON.parse(atob(encoded.replace(/-/g, '+').replace(/_/g, '/')));
    } catch {
      throw new AuthRequestError(502, 'The access token response is invalid.');
    }
    const now = Math.floor(this.dependencies.now() / 1000);
    if (
      claims.uid !== user.uid ||
      claims.sub !== user.uid ||
      typeof claims.sid !== 'string' ||
      !claims.sid ||
      !Number.isSafeInteger(claims.exp) ||
      !Number.isSafeInteger(claims.iat) ||
      Number(claims.iat) > now ||
      Number(claims.exp) <= now ||
      Number(claims.exp) - Number(claims.iat) > 3600
    ) {
      throw new AuthRequestError(
        502,
        'The access token response does not match this session.',
      );
    }
    this.jwt = { token: body.token, expires: Number(claims.exp) * 1000 };
    return body.token;
  }
}

let browserSession: AuthSession | undefined;
export function authSession(): AuthSession {
  if (typeof window === 'undefined')
    throw new Error('Authentication requires a browser.');
  browserSession ??= new AuthSession(webProfile(), {
    fetch: globalThis.fetch.bind(globalThis),
    storage: window.sessionStorage,
    now: () => Date.now(),
  });
  return browserSession;
}
