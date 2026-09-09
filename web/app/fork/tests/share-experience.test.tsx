import { afterEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AuthProvider } from '../../src/lib/fork/auth-context';
import { AuthSession, type AuthFetch } from '../../src/lib/fork/auth-session';
import {
  acceptSharedTasks,
  loadSharePreview,
  ShareRequestError,
} from '../../src/lib/fork/share-client';
import { ShareExperience } from '../../src/components/fork/ShareExperience';
import { parseWebProfile } from '../../src/lib/fork/web-profile';
import {
  GET as publicProxyGet,
  isPublicProxyPath,
  publicProxyCacheControl,
} from '../overlays/public-proxy-route';

const TOKEN = 'a'.repeat(32);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

function profile() {
  return parseWebProfile(
    JSON.stringify({
      name: 'cloudflare.local',
      target: 'cloudflare',
      stage: 'local',
      identity_provider: 'better_auth',
      api_base_url: 'http://127.0.0.1:8787',
      auth_base_url: 'http://127.0.0.1:8788',
      web_base_url: 'http://127.0.0.1:8789',
      mcp_base_url: 'http://127.0.0.1:8787',
      share_base_url: 'http://127.0.0.1:8789',
      objects_base_url: 'http://127.0.0.1:8787',
      auth_callback_scheme: 'fixture-dev',
      capabilities: { push_provider: 'webhook' },
    }),
  );
}

function session(fetch: AuthFetch) {
  const stored = new Map<string, string>();
  return new AuthSession(profile(), {
    fetch,
    now: () => Date.now(),
    storage: {
      getItem: (key) => stored.get(key) ?? null,
      setItem: (key, value) => stored.set(key, value),
      removeItem: (key) => stored.delete(key),
    },
  });
}

const taskPreview = {
  sender_name: 'Fixture Sender',
  tasks: [{ description: 'Keep Omi literal in user content', due_at: null }],
  count: 1,
  sender_uid: 'must-not-render',
};

describe('the share API consumer', () => {
  test('uses only the same-origin public/authenticated relays and strips private fields', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(Response.json(taskPreview))
      .mockResolvedValueOnce(
        Response.json({
          created: ['copied'],
          count: 1,
          private: 'not returned',
        }),
      );
    await expect(loadSharePreview('tasks', TOKEN, { fetch: fetcher })).resolves.toEqual({
      kind: 'tasks',
      senderName: 'Fixture Sender',
      tasks: [{ description: 'Keep Omi literal in user content', dueAt: null }],
    });
    await expect(acceptSharedTasks(TOKEN, 'jwt', { fetch: fetcher })).resolves.toEqual({
      count: 1,
    });
    expect(fetcher.mock.calls[0][0]).toBe(
      `/api/proxy/public/v1/action-items/shared/${TOKEN}`,
    );
    expect(fetcher.mock.calls[1]).toEqual([
      '/api/proxy/v1/action-items/accept',
      expect.objectContaining({
        body: JSON.stringify({ token: TOKEN }),
        headers: expect.objectContaining({ Authorization: 'Bearer jwt' }),
      }),
    ]);
  });

  test.each([
    [400, 'self'],
    [401, 'sign-in'],
    [402, 'locked'],
    [404, 'unavailable'],
    [409, 'already-accepted'],
    [503, 'temporary'],
  ] as const)(
    'maps task acceptance status %s without accepting it',
    async (status, code) => {
      const failure = acceptSharedTasks(TOKEN, 'jwt', {
        fetch: async () => Response.json({}, { status }),
      });
      await expect(failure).rejects.toMatchObject({ code });
    },
  );

  test('rejects invalid tokens and malformed previews before displaying content', async () => {
    const fetcher = vi.fn();
    await expect(
      loadSharePreview('chat', '../private', { fetch: fetcher }),
    ).rejects.toMatchObject({ code: 'invalid' });
    expect(fetcher).not.toHaveBeenCalled();
    await expect(
      loadSharePreview('tasks', TOKEN, {
        fetch: async () => Response.json({ ...taskPreview, count: 2 }),
      }),
    ).rejects.toBeInstanceOf(ShareRequestError);
    await expect(
      loadSharePreview('tasks', TOKEN, {
        fetch: async () => Response.json({ sender_name: 'Sender', tasks: [], count: 0 }),
      }),
    ).rejects.toMatchObject({ code: 'unavailable' });
  });
});

describe('the public relay overlay', () => {
  test('admits only exact share preview paths and never caches capability content', async () => {
    expect(isPublicProxyPath(`v1/action-items/shared/${TOKEN}`)).toBe(true);
    expect(isPublicProxyPath(`v2/messages/shared/${TOKEN}`)).toBe(true);
    expect(isPublicProxyPath(`v1/action-items/shared/${TOKEN}/extra`)).toBe(false);
    expect(isPublicProxyPath('v1/action-items/accept')).toBe(false);
    expect(publicProxyCacheControl(`v1/action-items/shared/${TOKEN}`, true)).toBe(
      'private, no-store',
    );
    vi.stubEnv('NEXT_PUBLIC_API_BASE_URL', 'https://api.example.invalid/root/');
    const fetcher = vi.fn(async (_input: RequestInfo | URL) =>
      Response.json(taskPreview, { headers: { 'cache-control': 'public' } }),
    );
    vi.stubGlobal('fetch', fetcher);
    const response = await publicProxyGet(
      new Request(
        `https://share.example.invalid/api/proxy/public/v1/action-items/shared/${TOKEN}`,
      ),
    );
    expect(new Request(fetcher.mock.calls[0][0]).url).toBe(
      `https://api.example.invalid/root/v1/action-items/shared/${TOKEN}`,
    );
    expect(response.headers.get('cache-control')).toBe('private, no-store');
    expect(response.headers.get('referrer-policy')).toBe('no-referrer');
    expect(await response.json()).toEqual({
      sender_name: 'Fixture Sender',
      tasks: [{ description: 'Keep Omi literal in user content', due_at: null }],
      count: 1,
    });
  });

  test('replaces private upstream fields and malformed share bodies at the relay boundary', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_BASE_URL', 'https://api.example.invalid');
    vi.stubGlobal('fetch', async () => Response.json(taskPreview));
    const controlled = await publicProxyGet(
      new Request(
        `https://share.example.invalid/api/proxy/public/v1/action-items/shared/${TOKEN}`,
      ),
    );
    expect(await controlled.text()).not.toContain('sender_uid');

    vi.stubGlobal('fetch', async () =>
      Response.json({ sender_name: 'Sender', tasks: [], count: 0 }),
    );
    const empty = await publicProxyGet(
      new Request(
        `https://share.example.invalid/api/proxy/public/v1/action-items/shared/${TOKEN}`,
      ),
    );
    expect(empty.status).toBe(404);
    expect(empty.headers.get('cache-control')).toBe('private, no-store');
  });

  test('uses the request-scoped Cloudflare Edge binding without external fetch', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_BASE_URL', 'https://api.example.invalid');
    const external = vi.fn();
    vi.stubGlobal('fetch', external);
    const timeout = vi.spyOn(AbortSignal, 'timeout');
    const binding = {
      fetch: vi.fn(async (_request: Request) => Response.json(taskPreview)),
    };
    const response = await publicProxyGet(
      new Request(
        `https://share.example.invalid/api/proxy/public/v1/action-items/shared/${TOKEN}`,
      ),
      binding,
    );
    expect(response.status).toBe(200);
    expect(binding.fetch).toHaveBeenCalledTimes(1);
    expect(external).not.toHaveBeenCalled();
    expect(timeout).not.toHaveBeenCalled();
    expect((binding.fetch.mock.calls[0][0] as Request).redirect).toBe('follow');
  });
});

describe('the visible task share flow', () => {
  test('previews anonymously, reuses Better Auth sign-in, then accepts once', async () => {
    vi.stubEnv('NEXT_PUBLIC_OMI_PRODUCT_NAME', 'Fixture Notebook');
    const now = Math.floor(Date.now() / 1000);
    const jwt = `header.${btoa(
      JSON.stringify({
        uid: 'recipient',
        sub: 'recipient',
        sid: 'session',
        iat: now,
        exp: now + 3600,
      }),
    )}.signature`;
    const authFetch = vi.fn<AuthFetch>(async (input) => {
      const url = String(input);
      if (url.endsWith('/sign-in/email'))
        return Response.json(
          { user: { id: 'recipient', name: 'Recipient' } },
          { headers: { 'set-auth-token': 'opaque-session' } },
        );
      if (url.endsWith('/token')) return Response.json({ token: jwt });
      throw new Error(`unexpected auth request ${url}`);
    });
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/shared/')) return Response.json(taskPreview);
      if (url.endsWith('/accept')) return Response.json({ created: ['copy'], count: 1 });
      throw new Error(`unexpected request ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);

    render(
      <AuthProvider session={session(authFetch)}>
        <ShareExperience kind="tasks" token={TOKEN} />
      </AuthProvider>,
    );
    expect(
      await screen.findByText('Keep Omi literal in user content'),
    ).toBeInTheDocument();
    expect(screen.queryByText('must-not-render')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Sign in to add tasks' }));
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'recipient@example.invalid' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'password' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Add to my tasks' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add to my tasks' }));
    expect(await screen.findByText('1 task was added')).toBeInTheDocument();
    expect(authFetch).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenCalledWith(
      '/api/proxy/v1/action-items/accept',
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: `Bearer ${jwt}`,
        }),
      }),
    );
  });

  test('shows expired and malformed links without opening sign-in', async () => {
    vi.stubEnv('NEXT_PUBLIC_OMI_PRODUCT_NAME', 'Fixture Notebook');
    const authFetch = vi.fn<AuthFetch>();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({}, { status: 404 })),
    );
    const view = render(
      <AuthProvider session={session(authFetch)}>
        <ShareExperience kind="chat" token={TOKEN} />
      </AuthProvider>,
    );
    expect(await screen.findByText('This private link has expired')).toBeInTheDocument();
    expect(screen.queryByLabelText('Email')).toBeNull();
    view.rerender(
      <AuthProvider session={session(authFetch)}>
        <ShareExperience kind="tasks" token="bad/token" />
      </AuthProvider>,
    );
    expect(await screen.findByText('This private link has expired')).toBeInTheDocument();
  });
});
