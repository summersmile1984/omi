import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { LoginClient } from '../overlays/LoginClient';
import { desktopDownloadUrl } from '../../src/lib/fork/referrals';
import { POST } from '../overlays/referral-claim-route';

const state = vi.hoisted(() => ({
  user: { uid: 'recipient' } as { uid: string } | null,
  params: new URLSearchParams(
    'referral=ref1.fixture.signature&environment=https://foreign.test',
  ),
  getToken: vi.fn(async () => 'selected-token'),
  push: vi.fn(),
  download: vi.fn(),
}));
vi.mock('@/lib/fork/referrals', async (original) => {
  const actual = await original<typeof import('../../src/lib/fork/referrals')>();
  return {
    ...actual,
    navigateToDesktopDownload: () => state.download(actual.desktopDownloadUrl()),
  };
});
vi.mock('@tschk/moonshine-next/navigation', () => ({
  useRouter: () => ({ push: state.push }),
  useSearchParams: () => state.params,
}));
vi.mock('@/lib/fork/auth-context', () => ({
  useAuth: () => ({ user: state.user, loading: false, getToken: state.getToken }),
}));
vi.mock('@/components/fork/LoginForm', () => ({ LoginForm: () => <span>Sign in</span> }));

function setup() {
  state.user = { uid: 'recipient' };
  state.params = new URLSearchParams(
    'referral=ref1.fixture.signature&environment=https://foreign.test',
  );
  state.push.mockClear();
  vi.stubEnv(
    'NEXT_PUBLIC_OMI_PROFILE_JSON',
    JSON.stringify({
      name: 'cloudflare.local',
      target: 'cloudflare',
      stage: 'local',
      identity_provider: 'better_auth',
      auth_callback_scheme: 'eddy',
      capabilities: { push_provider: 'webhook' },
      api_base_url: 'http://127.0.0.1:8100',
      auth_base_url: 'http://127.0.0.1:8101',
      web_base_url: 'http://127.0.0.1:8102',
      mcp_base_url: 'http://127.0.0.1:8100',
      share_base_url: 'http://127.0.0.1:8102',
      objects_base_url: 'http://127.0.0.1:8100',
    }),
  );
  const fetch = vi.fn();
  vi.stubGlobal('fetch', fetch);
  return fetch;
}
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test('signed-in referral retries remain visible and use only the selected API and download origin', async () => {
  const fetch = setup();
  fetch.mockResolvedValueOnce(Response.json({ error: 'unavailable' }, { status: 503 }));
  fetch.mockResolvedValueOnce(Response.json({ claimed: false, trial_days: 30 }));
  render(<LoginClient />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Please try again');
  expect(state.push).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Retry referral' }));
  await waitFor(() =>
    expect(screen.getByRole('alert')).toHaveTextContent('only available to new accounts'),
  );
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch).toHaveBeenLastCalledWith(
    'http://127.0.0.1:8100/v1/users/me/referral/claim',
    expect.objectContaining({
      credentials: 'omit',
      headers: {
        Authorization: 'Bearer selected-token',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ code: 'ref1.fixture.signature' }),
    }),
  );
  expect(desktopDownloadUrl()).toBe('http://127.0.0.1:8100/v2/desktop/download/latest');
  fireEvent.click(screen.getByRole('button', { name: 'Continue to your account' }));
  expect(state.push).toHaveBeenCalledWith('/home');
});

test('normal login keeps its home destination and signed-out referral makes no claim', async () => {
  const fetch = setup();
  state.user = null;
  const view = render(<LoginClient />);
  expect(screen.getByText('Sign in')).toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
  state.user = { uid: 'recipient' };
  state.params = new URLSearchParams();
  view.rerender(<LoginClient />);
  await waitFor(() => expect(state.push).toHaveBeenCalledWith('/home'));
});

test('legacy Web claim proxy cannot send the bearer to a query-selected backend', async () => {
  const fetch = setup();
  fetch.mockResolvedValue(Response.json({ claimed: true, trial_days: 30 }));
  const response = await POST(
    new Request('http://web.local/api/referrals/claim', {
      method: 'POST',
      headers: {
        Authorization: 'Bearer selected-token',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        code: 'ref1.fixture.signature',
        environment: 'https://foreign.test',
      }),
    }),
  );
  expect(response.status).toBe(200);
  expect(fetch).toHaveBeenCalledWith(
    'http://127.0.0.1:8100/v1/users/me/referral/claim',
    expect.objectContaining({ redirect: 'error' }),
  );
  expect(
    (await POST(new Request('http://web.local/api/referrals/claim', { method: 'POST' })))
      .status,
  ).toBe(401);
});

test('a completed claim stays visibly successful when the download does not navigate', async () => {
  const fetch = setup();
  state.download.mockClear();
  fetch.mockResolvedValue(Response.json({ claimed: true, trial_days: 30 }));
  render(<LoginClient />);
  expect(
    await screen.findByRole('heading', { name: 'Your 30-day Operator trial is ready.' }),
  ).toBeInTheDocument();
  expect(state.download).toHaveBeenCalledWith(
    'http://127.0.0.1:8100/v2/desktop/download/latest',
  );
  expect(screen.getByRole('link', { name: 'Download the desktop app' })).toHaveAttribute(
    'href',
    'http://127.0.0.1:8100/v2/desktop/download/latest',
  );
  expect(screen.queryByRole('status')).toBeNull();
});
