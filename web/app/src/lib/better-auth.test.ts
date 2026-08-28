import { afterEach, describe, expect, it, vi } from 'vitest';
import { getBetterAuthToken, signInWithEmail, signUpWithEmail } from './better-auth';

const token = 'eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJzdGFnaW5nLXVzZXIifQ.';

describe('Better Auth web client', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('signs up through the same-origin Worker proxy and exposes the bearer token', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          token,
          user: {
            id: 'staging-user',
            name: 'Staging User',
            email: 'staging@example.invalid',
          },
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ),
    );

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
    expect(await getBetterAuthToken()).toBe(token);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/better-auth/sign-up/email',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
      }),
    );
  });

  it('surfaces the Better Auth error message without exposing response details', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ message: 'Invalid email or password' }), {
        status: 401,
        headers: { 'content-type': 'application/json' },
      }),
    );

    await expect(
      signInWithEmail('staging@example.invalid', 'wrong-password'),
    ).rejects.toThrow('Invalid email or password');
  });
});
