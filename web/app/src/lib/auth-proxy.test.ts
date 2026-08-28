import { describe, expect, it } from 'vitest';
import { sanitizeBetterAuthResponse } from './auth-proxy';

describe('sanitizeBetterAuthResponse', () => {
  it('removes the browser-readable session token from successful sign-in', () => {
    const response = Response.json({ token: 'secret', user: { id: 'user-1' } });
    expect(
      JSON.parse(
        sanitizeBetterAuthResponse(
          'sign-in/email',
          response,
          JSON.stringify({ token: 'secret', user: { id: 'user-1' } }),
        ) || '{}',
      ),
    ).toEqual({ user: { id: 'user-1' } });
  });

  it('fails closed on malformed successful auth JSON', () => {
    const response = new Response('{', {
      headers: { 'content-type': 'application/json' },
    });
    expect(sanitizeBetterAuthResponse('sign-up/email', response, '{')).toBeNull();
  });
});
