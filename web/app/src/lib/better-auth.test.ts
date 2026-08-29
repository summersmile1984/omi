import { afterEach, describe, expect, it, vi } from 'vitest';

describe('Better Auth web client', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.resetModules();
    vi.unstubAllGlobals();
  });

  it('signs up through the same-origin proxy without retaining the session token', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          token: 'must-not-be-retained',
          user: {
            id: 'staging-user',
            name: 'Staging User',
            email: 'staging@example.invalid',
          },
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ),
    );
    const { signUpWithEmail } = await import('./better-auth');

    const user = await signUpWithEmail(
      'Staging User',
      'staging@example.invalid',
      'password-123',
    );

    expect(user).toMatchObject({
      uid: 'staging-user',
      displayName: 'Staging User',
      email: 'staging@example.invalid',
    });
    expect(user).not.toBeNull();
    expect(await user?.getIdToken?.()).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/better-auth/sign-up/email',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        headers: expect.any(Headers),
      }),
    );
    const request = fetchMock.mock.calls[0][1];
    expect(new Headers(request?.headers).has('authorization')).toBe(false);
  });

  it('restores identity from the httpOnly session cookie', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      Response.json({
        session: { id: 'session-1' },
        user: { id: 'cookie-user', email: 'cookie@example.invalid' },
      }),
    );
    const { onBetterAuthStateChange } = await import('./better-auth');
    const user = await new Promise<{ uid: string } | null>((resolve) => {
      const unsubscribe = onBetterAuthStateChange((value) => {
        unsubscribe();
        resolve(value);
      });
    });

    expect(user?.uid).toBe('cookie-user');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/better-auth/get-session',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('surfaces the Better Auth error message without exposing response details', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ message: 'Invalid email or password' }), {
        status: 401,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const { signInWithEmail } = await import('./better-auth');

    await expect(
      signInWithEmail('staging@example.invalid', 'wrong-password'),
    ).rejects.toThrow('Invalid email or password');
  });

  it('exposes only configured supported social providers', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      Response.json({ social_providers: ['google', 'unknown', 'apple'] }),
    );
    const { getBetterAuthSocialProviders } = await import('./better-auth');

    await expect(getBetterAuthSocialProviders()).resolves.toEqual(['google', 'apple']);
  });

  it('starts social sign-in through the same-origin callback path', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        Response.json({ url: 'https://accounts.google.com/o/oauth2/v2/auth' }),
      );
    const { getBetterAuthSocialSignInUrl } = await import('./better-auth');

    await expect(getBetterAuthSocialSignInUrl('google')).resolves.toBe(
      'https://accounts.google.com/o/oauth2/v2/auth',
    );
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/better-auth/sign-in/social',
      expect.objectContaining({ method: 'POST', credentials: 'include' }),
    );
    const request = fetchMock.mock.calls[0][1];
    expect(JSON.parse(String(request?.body))).toEqual({
      provider: 'google',
      callbackURL: '/conversations',
      errorCallbackURL: '/login',
    });
  });

  it('rejects an OAuth redirect outside the configured provider', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      Response.json({ url: 'https://attacker.example.test/authorize' }),
    );
    const { getBetterAuthSocialSignInUrl } = await import('./better-auth');

    await expect(getBetterAuthSocialSignInUrl('google')).rejects.toThrow(
      'untrusted OAuth redirect',
    );
  });

  it('forwards only the server-signed OAuth query through email login', async () => {
    vi.stubGlobal('window', {
      location: {
        origin: 'https://web.test',
        search:
          '?client_id=mcp-client&scope=memories.read&unsigned=drop-me&sig=signed&ba_param=client_id&ba_param=scope&ba_param=exp&ba_param=sig&ba_param=ba_param&exp=9999999999',
        assign: vi.fn(),
      },
    });
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      Response.json({
        user: { id: 'oauth-user', email: 'oauth@example.invalid' },
      }),
    );
    const { signInWithEmail } = await import('./better-auth');

    await signInWithEmail('oauth@example.invalid', 'password-123');

    const request = fetchMock.mock.calls[0][1];
    const body = JSON.parse(String(request?.body));
    expect(body.oauth_query).toContain('client_id=mcp-client');
    expect(body.oauth_query).toContain('scope=memories.read');
    expect(body.oauth_query).not.toContain('unsigned=drop-me');
  });

  it('loads public client metadata and submits a signed consent decision', async () => {
    vi.stubGlobal('window', {
      location: { origin: 'https://web.test', search: '', assign: vi.fn() },
    });
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        Response.json({
          client_id: 'mcp-client',
          client_name: 'Claude Desktop',
          client_uri: 'https://client.example',
        }),
      )
      .mockResolvedValueOnce(
        Response.json({
          redirect: true,
          url: 'http://127.0.0.1:8123/callback?code=opaque',
        }),
      );
    const { getBetterAuthOAuthClient, submitBetterAuthOAuthConsent } =
      await import('./better-auth');

    await expect(getBetterAuthOAuthClient('mcp-client', 'signed-query')).resolves.toEqual(
      {
        clientId: 'mcp-client',
        name: 'Claude Desktop',
        uri: 'https://client.example',
        icon: null,
      },
    );
    await expect(submitBetterAuthOAuthConsent(true, 'signed-query')).resolves.toBe(
      'http://127.0.0.1:8123/callback?code=opaque',
    );
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      accept: true,
      oauth_query: 'signed-query',
    });
  });
});
