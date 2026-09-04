import { describe, expect, test } from 'bun:test';
import {
  AuthRequestError,
  AuthSession,
  type AuthFetch,
} from '../../src/lib/fork/auth-session';
import { parseWebProfile } from '../../src/lib/fork/web-profile';

function profile(target = 'self_hosted', authOrigin = 'https://auth.fixture.invalid') {
  return parseWebProfile(
    JSON.stringify({
      name: `${target}.production`,
      target,
      stage: 'production',
      identity_provider: 'better_auth',
      api_base_url: 'https://api.fixture.invalid/',
      auth_base_url: authOrigin,
      web_base_url: 'https://web.fixture.invalid',
      mcp_base_url: 'https://mcp.fixture.invalid',
      share_base_url: 'https://share.fixture.invalid',
      objects_base_url: 'https://objects.fixture.invalid',
      auth_callback_scheme: 'fixture',
      capabilities: { push_provider: 'webhook' },
    }),
  );
}

function fixture(target = 'self_hosted') {
  const stored = new Map<string, string>();
  const requests: Array<{ url: string; init?: RequestInit }> = [];
  let clock = 1_800_000_000_000;
  let state = 0;
  let accessStatus = 200;
  let tokenHook: (() => Promise<void>) | undefined;
  const storage = {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => {
      stored.set(key, value);
    },
    removeItem: (key: string) => {
      stored.delete(key);
    },
  };
  // The client decodes only for cache lifetime, never as cryptographic proof.
  const projection = () =>
    `header.${btoa(
      JSON.stringify({
        sub: 'existing-user',
        uid: 'existing-user',
        sid: `session-${state}`,
        iat: Math.floor(clock / 1000),
        exp: Math.floor(clock / 1000) + 3600,
      }),
    )}.signature`;
  const transport: AuthFetch = async (input, init) => {
    const url = String(input);
    requests.push({ url, init });
    if (/sign-(in|up)\/email$/.test(url)) {
      state++;
      return Response.json(
        { user: { id: 'existing-user', name: 'Fixture User' } },
        {
          headers: { 'set-auth-token': `opaque-session-${state}` },
        },
      );
    }
    if (url.endsWith('/get-session'))
      return Response.json({ user: { id: 'existing-user', name: 'Fixture User' } });
    if (url.endsWith('/sign-out')) return Response.json({ success: true });
    if (url.endsWith('/token')) {
      await tokenHook?.();
      if (accessStatus !== 200)
        return Response.json({ error: 'synthetic' }, { status: accessStatus });
      return Response.json({ token: projection() });
    }
    throw new Error(`Unexpected fixture request ${url}`);
  };
  const config = profile(target);
  const client = new AuthSession(config, { fetch: transport, storage, now: () => clock });
  return {
    client,
    config,
    storage,
    stored,
    requests,
    transport,
    advance: (milliseconds: number) => {
      clock += milliseconds;
    },
    clock: () => clock,
    status: (status: number) => {
      accessStatus = status;
    },
    hook: (callback?: () => Promise<void>) => {
      tokenHook = callback;
    },
  };
}

describe('one session/access-token contract for both targets', () => {
  for (const target of ['self_hosted', 'cloudflare']) {
    test(`${target}: signup persists only the opaque session and exchanges a separate JWT`, async () => {
      const f = fixture(target);
      await f.client.signUp(
        'Fixture User',
        'fixture@example.invalid',
        'synthetic-password',
      );
      expect(f.client.currentUser?.uid).toBe('existing-user');
      expect([...f.stored.values()]).toEqual(['opaque-session-1']);
      expect(await f.client.getToken()).toContain('.signature');
      expect(f.requests.at(-1)?.init?.headers).toEqual({
        authorization: 'Bearer opaque-session-1',
      });
      expect(
        f.requests.every((request) =>
          request.url.startsWith('https://auth.fixture.invalid/api/auth/'),
        ),
      ).toBe(true);
      expect(f.requests.every((request) => request.init?.credentials === 'omit')).toBe(
        true,
      );
      const restored = new AuthSession(f.config, {
        fetch: f.transport,
        storage: f.storage,
        now: f.clock,
      });
      await restored.restore();
      expect(restored.currentUser?.uid).toBe('existing-user');
      expect(f.requests.at(-1)?.url.endsWith('/get-session')).toBe(true);
    });
  }

  test('concurrent API and realtime requests share one refresh and refresh before expiry', async () => {
    const f = fixture();
    await f.client.signIn('fixture@example.invalid', 'synthetic-password');
    const tokens = await Promise.all([
      f.client.getToken(),
      f.client.getToken(),
      f.client.getToken(),
    ]);
    expect(new Set(tokens).size).toBe(1);
    expect(f.requests.filter((request) => request.url.endsWith('/token'))).toHaveLength(
      1,
    );
    await f.client.getToken();
    expect(f.requests.filter((request) => request.url.endsWith('/token'))).toHaveLength(
      1,
    );
    f.advance(3540_000);
    expect(await f.client.getToken()).not.toBe(tokens[0]);
    expect(f.requests.filter((request) => request.url.endsWith('/token'))).toHaveLength(
      2,
    );
  });

  test('authority outages preserve credentials while revocation clears them', async () => {
    const f = fixture();
    await f.client.signIn('fixture@example.invalid', 'synthetic-password');
    f.status(503);
    await expect(f.client.getToken()).rejects.toMatchObject({ status: 503 });
    expect(f.stored.size).toBe(1);
    expect(f.client.currentUser?.uid).toBe('existing-user');
    f.status(401);
    await expect(f.client.getToken()).rejects.toMatchObject({ status: 401 });
    expect(f.stored.size).toBe(0);
    expect(f.client.currentUser).toBeNull();
  });

  test('logout does not allow an in-flight JWT response to restore the old identity', async () => {
    const f = fixture();
    await f.client.signIn('fixture@example.invalid', 'synthetic-password');
    let finish!: () => void;
    f.hook(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const pending = f.client.getToken();
    await Promise.resolve();
    await Promise.resolve();
    await f.client.signOut();
    finish();
    expect(await pending).toBeNull();
    expect(await f.client.getToken()).toBeNull();
    expect(f.stored.size).toBe(0);
  });

  test('a new sign-in gets its own refresh even when the previous session exchange is pending', async () => {
    const f = fixture();
    await f.client.signIn('fixture@example.invalid', 'synthetic-password');
    let finish!: () => void;
    f.hook(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const old = f.client.getToken();
    await Promise.resolve();
    await Promise.resolve();
    await f.client.signIn('fixture@example.invalid', 'synthetic-password');
    f.hook();
    expect(await f.client.getToken()).toContain('.signature');
    expect(f.requests.at(-1)?.init?.headers).toEqual({
      authorization: 'Bearer opaque-session-2',
    });
    finish();
    expect(await old).toBeNull();
  });

  test('a missing exposed session header cannot become a false successful login', async () => {
    const f = fixture();
    const client = new AuthSession(f.config, {
      storage: f.storage,
      now: f.clock,
      fetch: async () => Response.json({ user: { id: 'existing-user' } }),
    });
    await expect(
      client.signIn('fixture@example.invalid', 'synthetic-password'),
    ).rejects.toBeInstanceOf(AuthRequestError);
    expect(client.currentUser).toBeNull();
    expect(f.stored.size).toBe(0);
  });
});

test('different authorities and targets never share a stored credential key', () => {
  const f = fixture();
  const another = new AuthSession(profile('cloudflare'), {
    storage: f.storage,
    fetch: f.transport,
    now: f.clock,
  });
  const anotherOrigin = new AuthSession(profile('self_hosted', 'https://other.invalid'), {
    storage: f.storage,
    fetch: f.transport,
    now: f.clock,
  });
  expect(
    new Set([f.client.storageKey, another.storageKey, anotherOrigin.storageKey]).size,
  ).toBe(3);
});

test('profile selection rejects missing targets, implicit auth origins and nonlocal HTTP', () => {
  expect(() => parseWebProfile(undefined)).toThrow();
  for (const patch of [
    { name: 'self_hosted' },
    { auth_base_url: '' },
    { target: 'omi_cloud' },
    { auth_base_url: 'https://auth.invalid/unreviewed-path' },
    { api_base_url: 'http://api.invalid/' },
    { capabilities: { push_provider: 'firebase' } },
  ]) {
    expect(() => parseWebProfile(JSON.stringify({ ...profile(), ...patch }))).toThrow();
  }
});
