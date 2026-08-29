import { describe, expect, it } from 'vitest';
import {
  betterAuthRequestHeaders,
  betterAuthResponseHeaders,
  betterAuthTarget,
  betterAuthUpstreamRequest,
  proxyBetterAuthMetadata,
  sanitizeBetterAuthResponse,
} from './auth-proxy';

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

  it('keeps the public Better Auth path and query for OAuth callbacks', () => {
    expect(
      betterAuthTarget(
        'callback/google',
        '?code=opaque&state=opaque',
        'https://auth.test',
      ).toString(),
    ).toBe('https://auth.test/api/better-auth/callback/google?code=opaque&state=opaque');
  });

  it('preserves OAuth redirects and cookies but disables response caching', () => {
    const upstream = new Response(null, {
      status: 302,
      headers: {
        location: 'https://accounts.example.test/authorize',
        'set-cookie': '__Secure-better-auth.state=opaque; HttpOnly; Secure',
        'cache-control': 'public, max-age=300',
      },
    });
    const headers = betterAuthResponseHeaders(upstream);

    expect(headers.get('location')).toBe('https://accounts.example.test/authorize');
    expect(headers.get('set-cookie')).toContain('__Secure-better-auth.state=opaque');
    expect(headers.get('cache-control')).toBe('no-store');
  });

  it('leaves OAuth redirects for the browser instead of following them in the Auth Worker', () => {
    const incoming = new Request(
      'https://web.test/api/better-auth/oauth2/authorize?client_id=client-1',
      {
        headers: {
          cookie: '__Secure-better-auth.session_token=opaque',
          'x-omi-auth-context': 'forged',
        },
      },
    );
    const upstream = betterAuthUpstreamRequest(
      incoming,
      betterAuthTarget(
        'oauth2/authorize',
        '?client_id=client-1',
        'https://auth.internal',
      ),
    );

    expect(upstream.redirect).toBe('manual');
    expect(upstream.url).toBe(
      'https://auth.internal/api/better-auth/oauth2/authorize?client_id=client-1',
    );
    expect(upstream.headers.get('cookie')).toContain('session_token');
    expect(upstream.headers.has('x-omi-auth-context')).toBe(false);
  });

  it('forwards Cloudflare client identity without trusting proxy chains', () => {
    const headers = betterAuthRequestHeaders(
      new Headers({
        'cf-connecting-ip': '192.0.2.10',
        cookie: '__Secure-better-auth.session_token=opaque',
        'x-forwarded-for': '198.51.100.7',
        'x-omi-auth-context': 'forged',
      }),
    );

    expect(headers.get('cf-connecting-ip')).toBe('192.0.2.10');
    expect(headers.get('cookie')).toContain('session_token');
    expect(headers.has('x-forwarded-for')).toBe(false);
    expect(headers.has('x-omi-auth-context')).toBe(false);
  });

  it('proxies both root and issuer-path OAuth metadata aliases without following redirects', async () => {
    const requests: Request[] = [];
    const authService = {
      fetch: async (request: Request) => {
        requests.push(request);
        return Response.json(
          { issuer: 'https://web.test/api/better-auth' },
          { headers: { 'cache-control': 'public, max-age=300' } },
        );
      },
    };

    const response = await proxyBetterAuthMetadata(
      new Request('https://web.test/.well-known/oauth-authorization-server'),
      authService,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      issuer: 'https://web.test/api/better-auth',
    });
    expect(response.headers.get('cache-control')).toBe('no-store');

    const head = await proxyBetterAuthMetadata(
      new Request('https://web.test/.well-known/oauth-authorization-server', {
        method: 'HEAD',
      }),
      authService,
    );
    expect(head.status).toBe(200);
    expect(await head.text()).toBe('');
    expect(requests.map((request) => request.method)).toEqual(['GET', 'GET']);
    expect(requests.every((request) => request.redirect === 'manual')).toBe(true);
    expect(requests.map((request) => new URL(request.url).pathname)).toEqual([
      '/api/better-auth/.well-known/oauth-authorization-server',
      '/api/better-auth/.well-known/oauth-authorization-server',
    ]);
  });
});
