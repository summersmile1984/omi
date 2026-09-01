import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./better-auth', () => ({ isBetterAuthEnabled: true }));
vi.mock('./firebase', () => ({ getIdToken: vi.fn() }));
vi.mock('./clientDevice', () => ({ getWebDeviceIdHash: vi.fn(async () => null) }));
vi.mock('./cache', () => ({
  CACHE_TTL: {},
  cacheKeys: {},
  fetchWithCache: vi.fn(),
  invalidateCache: vi.fn(),
  invalidationPatterns: {},
}));

import { sendMessageStream } from './api';

describe('Cloudflare chat attachment polling', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('adapts a durable 202 Assistant run back into Web data/done chunks', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: 'queued' }), {
          status: 202,
          headers: {
            'content-type': 'application/json',
            'x-omi-chat-stream': 'poll',
            location: '/v2/cf/chat-sessions/session-1/assistant-runs/run-1',
          },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: 'completed',
            result: { text: 'The file says hello.' },
            assistant_message_id: 'assistant-1',
          }),
        ),
      );
    vi.stubGlobal('fetch', fetchMock);

    const chunks: Array<{
      type: string;
      text: string;
      message?: { id: string; text: string };
    }> = [];
    await sendMessageStream(
      'Summarize this file',
      (chunk) => chunks.push(chunk as (typeof chunks)[number]),
      {
        fileIds: ['file-1'],
      },
    );

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe(
      '/api/proxy/v2/cf/chat-sessions/session-1/assistant-runs/run-1',
    );
    expect(chunks).toEqual([
      { type: 'data', text: 'The file says hello.' },
      {
        type: 'done',
        text: expect.any(String),
        message: expect.objectContaining({
          id: 'assistant-1',
          text: 'The file says hello.',
        }),
      },
    ]);
  });

  it('rejects an untrusted polling location before issuing a second request', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ status: 'queued' }), {
        status: 202,
        headers: {
          'x-omi-chat-stream': 'poll',
          location: 'https://attacker.example/run-1',
        },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      sendMessageStream('Summarize this file', () => undefined, { fileIds: ['file-1'] }),
    ).rejects.toThrow('Invalid chat assistant polling location');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
