import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AuthProvider, useAuth } from '../../src/lib/fork/auth-context';
import { AuthSession, type AuthFetch } from '../../src/lib/fork/auth-session';
import { LoginForm } from '../../src/components/fork/LoginForm';
import { parseWebProfile } from '../../src/lib/fork/web-profile';

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
});

function fixture() {
  vi.stubEnv('NEXT_PUBLIC_OMI_PRODUCT_NAME', 'Fixture Notebook');
  const profile = parseWebProfile(
    JSON.stringify({
      name: 'cloudflare.local',
      target: 'cloudflare',
      stage: 'local',
      identity_provider: 'better_auth',
      api_base_url: 'http://127.0.0.1:8100',
      auth_base_url: 'http://127.0.0.1:8788',
      web_base_url: 'http://127.0.0.1:8789',
      mcp_base_url: 'http://127.0.0.1:8101',
      share_base_url: 'http://127.0.0.1:8102',
      objects_base_url: 'http://127.0.0.1:8103',
      auth_callback_scheme: 'fixture-dev',
      capabilities: { push_provider: 'webhook' },
    }),
  );
  const stored = new Map<string, string>();
  const fetch = vi.fn<AuthFetch>();
  const session = new AuthSession(profile, {
    fetch,
    now: () => Date.now(),
    storage: {
      getItem: (key) => stored.get(key) ?? null,
      setItem: (key, value) => {
        stored.set(key, value);
      },
      removeItem: (key) => {
        stored.delete(key);
      },
    },
  });
  const onComplete = vi.fn();
  return { session, fetch, stored, onComplete };
}

function fillSignIn() {
  fireEvent.change(screen.getByLabelText('Email'), {
    target: { value: 'fixture@example.invalid' },
  });
  fireEvent.change(screen.getByLabelText('Password'), {
    target: { value: 'synthetic-password' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

test('the visible sign-in form calls the production session client and uses the selected brand', async () => {
  const f = fixture();
  f.fetch.mockResolvedValue(
    Response.json(
      { user: { id: 'fixture-user', name: 'Fixture User' } },
      {
        headers: { 'set-auth-token': 'opaque-session' },
      },
    ),
  );
  render(
    <AuthProvider session={f.session}>
      <LoginForm onComplete={f.onComplete} />
    </AuthProvider>,
  );
  expect(screen.getByText('Fixture Notebook')).toBeInTheDocument();
  expect(screen.queryByText(/Google|Apple|Firebase/)).toBeNull();
  fillSignIn();
  await waitFor(() => expect(f.onComplete).toHaveBeenCalledOnce());
  expect(f.fetch).toHaveBeenCalledWith(
    'http://127.0.0.1:8788/api/auth/sign-in/email',
    expect.objectContaining({
      method: 'POST',
      credentials: 'omit',
      body: JSON.stringify({
        email: 'fixture@example.invalid',
        password: 'synthetic-password',
      }),
    }),
  );
  expect(f.session.currentUser?.uid).toBe('fixture-user');
  expect([...f.stored.values()]).toEqual(['opaque-session']);
});

test('rejected credentials display an error without completing sign-in', async () => {
  const f = fixture();
  f.fetch.mockResolvedValue(
    Response.json({ error: 'invalid credentials' }, { status: 401 }),
  );
  render(
    <AuthProvider session={f.session}>
      <LoginForm onComplete={f.onComplete} />
    </AuthProvider>,
  );
  fillSignIn();
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Check your email and password',
  );
  expect(f.onComplete).not.toHaveBeenCalled();
  expect(f.stored.size).toBe(0);
});

test('a saved-session outage offers retry and recovers the same identity', async () => {
  const f = fixture();
  f.stored.set(f.session.storageKey, 'opaque-session');
  f.fetch.mockResolvedValueOnce(Response.json({ error: 'unavailable' }, { status: 503 }));
  f.fetch.mockResolvedValueOnce(Response.json({ user: { id: 'fixture-user' } }));
  function Identity() {
    const { user } = useAuth();
    return <span>{user?.uid ?? 'signed out'}</span>;
  }
  render(
    <AuthProvider session={f.session}>
      <LoginForm onComplete={f.onComplete} />
      <Identity />
    </AuthProvider>,
  );
  const retry = await screen.findByRole('button', { name: 'Retry saved session' });
  expect(f.stored.size).toBe(1);
  fireEvent.click(retry);
  expect(await screen.findByText('fixture-user')).toBeInTheDocument();
  expect(screen.queryByRole('alert')).toBeNull();
});
