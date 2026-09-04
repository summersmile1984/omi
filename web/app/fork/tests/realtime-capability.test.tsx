import { afterEach, expect, test, vi } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { rewriteRealtimeStart, rewriteRealtimeControl } from '../realtime-overlay';

const { issueToken, createClient, connect } = vi.hoisted(() => ({
  issueToken: vi.fn(async () => ({ token: 'synthetic-model-token' })),
  createClient: vi.fn(),
  connect: vi.fn(),
}));
vi.mock('@/lib/api', () => ({
  createGeminiLiveSession: issueToken,
  reportGeminiLiveUsage: vi.fn(),
}));
vi.mock('@/lib/geminiLive', () => ({
  GeminiLiveClient: class {
    constructor(...args: unknown[]) {
      createClient(...args);
    }
    connect(token: string) {
      connect(token);
    }
    stop() {}
  },
}));
// The fork Vitest plugin applies the same staging transform to this production
// module. This executes start(), not a source-string assertion about a gate.
import { useGeminiLive } from '@/hooks/useGeminiLive';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllEnvs();
});

function configure(enabled: boolean) {
  vi.stubEnv(
    'NEXT_PUBLIC_OMI_PROFILE_JSON',
    JSON.stringify({
      name: 'cloudflare.production',
      target: 'cloudflare',
      stage: 'production',
      identity_provider: 'better_auth',
      api_base_url: 'https://api.fixture.invalid',
      auth_base_url: 'https://auth.fixture.invalid',
      web_base_url: 'https://web.fixture.invalid',
      mcp_base_url: 'https://mcp.fixture.invalid',
      share_base_url: 'https://share.fixture.invalid',
      objects_base_url: 'https://objects.fixture.invalid',
      auth_callback_scheme: 'fixture',
      capabilities: { push_provider: 'webhook', allow_direct_model_providers: enabled },
    }),
  );
}

test('a disabled capability blocks the production hook before token, client or socket creation', async () => {
  configure(false);
  const { result } = renderHook(() =>
    useGeminiLive({ messages: [], onExchange: vi.fn() }),
  );
  await act(async () => {
    await result.current.start();
  });
  expect(result.current.state).toBe('idle');
  expect(result.current.error).toContain('unavailable for this deployment');
  expect(issueToken).not.toHaveBeenCalled();
  expect(createClient).not.toHaveBeenCalled();
  expect(connect).not.toHaveBeenCalled();
});

test('an enabled capability retains the upstream token and connection path', async () => {
  configure(true);
  const { result } = renderHook(() =>
    useGeminiLive({ messages: [], onExchange: vi.fn() }),
  );
  await act(async () => {
    await result.current.start();
  });
  expect(issueToken).toHaveBeenCalledOnce();
  expect(createClient).toHaveBeenCalledOnce();
  expect(connect).toHaveBeenCalledWith('synthetic-model-token');
});

test('the static transform contract refuses a changed or duplicate upstream owner', () => {
  const home = readFileSync(resolve('src/components/home/HomePage.tsx'), 'utf8');
  const hook = readFileSync(resolve('src/hooks/useGeminiLive.ts'), 'utf8');
  expect(() => rewriteRealtimeControl(home)).not.toThrow();
  expect(() =>
    rewriteRealtimeControl(home.replace('recording={{', 'changedRecording={{')),
  ).toThrow();
  expect(() => rewriteRealtimeControl(rewriteRealtimeControl(home))).toThrow();
  expect(() =>
    rewriteRealtimeStart(hook.replace('const start =', 'const changedStart =')),
  ).toThrow();
});
