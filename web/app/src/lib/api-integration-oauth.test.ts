import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./better-auth', () => ({ isBetterAuthEnabled: true }));
vi.mock('./clientDevice', () => ({ getWebDeviceIdHash: vi.fn(async () => null) }));
vi.mock('./firebase', () => ({ getIdToken: vi.fn(async () => null) }));
vi.mock('./cache', () => ({
  CACHE_TTL: {},
  cacheKeys: {},
  fetchWithCache: vi.fn(),
  invalidateCache: vi.fn(),
  invalidationPatterns: {},
}));

import { ApiRequestError, getIntegrationOAuthUrl } from './api';

describe('getIntegrationOAuthUrl', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('preserves a staging OAuth configuration failure for the settings UI', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json(
          { detail: 'Google Calendar is not configured' },
          { status: 503, statusText: 'Service Unavailable' },
        ),
      ),
    );

    await expect(getIntegrationOAuthUrl('google_calendar')).rejects.toMatchObject({
      name: 'ApiRequestError',
      status: 503,
      message: 'API error: 503 Service Unavailable',
    });
  });

  it('returns the provider URL when OAuth is configured', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json({ auth_url: 'https://accounts.google.com/o/oauth2/v2/auth' }),
      ),
    );

    await expect(getIntegrationOAuthUrl('google_calendar')).resolves.toBe(
      'https://accounts.google.com/o/oauth2/v2/auth',
    );
  });

  it('exports a typed request error for callers that need status-aware handling', () => {
    const error = new ApiRequestError(503, 'unavailable');
    expect(error).toBeInstanceOf(Error);
    expect(error.status).toBe(503);
    expect(error.name).toBe('ApiRequestError');
  });
});
