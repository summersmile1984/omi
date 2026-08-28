import { afterEach, describe, expect, it, vi } from 'vitest';

describe('Better Auth web client', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.resetModules();
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
    expect(await user.getIdToken?.()).toBeNull();
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
});
