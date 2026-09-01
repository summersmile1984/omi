import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./better-auth', () => ({ isBetterAuthEnabled: true }));
vi.mock('./firebase', () => ({ getIdToken: vi.fn(async () => null) }));
vi.mock('./clientDevice', () => ({ getWebDeviceIdHash: vi.fn(async () => null) }));
vi.mock('./cache', () => ({
  CACHE_TTL: {},
  cacheKeys: {},
  fetchWithCache: vi.fn(),
  invalidateCache: vi.fn(),
  invalidationPatterns: {},
}));

import { getNotificationScopes } from './api';

describe('app metadata routes', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('requests notification scopes from the Cloudflare canonical singular app route', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        Response.json([{ id: 'user_context', title: 'User Conversations' }]),
      );
    vi.stubGlobal('fetch', fetchMock);

    await expect(getNotificationScopes()).resolves.toEqual([
      { id: 'user_context', title: 'User Conversations' },
    ]);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/proxy/v1/app/proactive-notification-scopes',
      expect.objectContaining({ credentials: 'include' }),
    );
    const requestOptions = fetchMock.mock.calls[0][1];
    expect(requestOptions?.headers).toBeInstanceOf(Headers);
    expect(new Headers(requestOptions?.headers).get('X-App-Platform')).toBe('web');
  });
});
