import { describe, expect, it, vi } from 'vitest';

import { proxyApiRequest, type WorkerService } from './api-proxy';

const requestUrl = 'https://web.example/api/proxy/v1/conversations?limit=10';

describe('proxyApiRequest', () => {
  it('uses the Edge service binding and preserves streaming bodies and contract headers', async () => {
    let captured: Request | undefined;
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: first\n\n'));
        controller.close();
      },
    });
    const service: WorkerService = {
      fetch: vi.fn(async (request) => {
        captured = request;
        return new Response(stream, {
          status: 206,
          headers: {
            'content-type': 'text/event-stream',
            'content-range': 'bytes 0-12/13',
            etag: 'asset-etag',
            'retry-after': '2',
            'x-request-id': 'edge-request',
            connection: 'keep-alive',
          },
        });
      }),
    };
    const networkFetch = vi.fn<typeof fetch>();

    const response = await proxyApiRequest(
      new Request(requestUrl, {
        headers: {
          authorization: 'Bearer session-token',
          range: 'bytes=0-12',
          'if-none-match': 'client-etag',
          'x-app-platform': 'web',
        },
      }),
      { path: ['v1', 'conversations'] },
      {
        service,
        upstreamBaseUrl: 'https://edge.internal',
        fetchImpl: networkFetch,
      },
    );

    expect(service.fetch).toHaveBeenCalledOnce();
    expect(networkFetch).not.toHaveBeenCalled();
    expect(captured?.url).toBe('https://edge.internal/v1/conversations?limit=10');
    expect(captured?.headers.get('authorization')).toBe('Bearer session-token');
    expect(captured?.headers.get('range')).toBe('bytes=0-12');
    expect(captured?.headers.get('if-none-match')).toBe('client-etag');
    expect(response.status).toBe(206);
    expect(response.headers.get('content-range')).toBe('bytes 0-12/13');
    expect(response.headers.get('etag')).toBe('asset-etag');
    expect(response.headers.get('retry-after')).toBe('2');
    expect(response.headers.get('x-request-id')).toBe('edge-request');
    expect(response.headers.has('connection')).toBe(false);
    expect(await response.text()).toBe('data: first\n\n');
  });

  it('retains direct fetch only as the non-Worker fallback', async () => {
    const fetchImpl = vi.fn(async () => Response.json({ ok: true }, { status: 200 }));
    const response = await proxyApiRequest(
      new Request(requestUrl, {
        headers: { authorization: 'Bearer local-token' },
      }),
      { path: ['v1', 'conversations'] },
      { upstreamBaseUrl: 'https://api.example', fetchImpl },
    );

    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(await response.json()).toEqual({ ok: true });
  });

  it('fails closed before calling upstream without a bearer token', async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const response = await proxyApiRequest(
      new Request(requestUrl),
      { path: ['v1', 'conversations'] },
      { upstreamBaseUrl: 'https://api.example', fetchImpl },
    );

    expect(response.status).toBe(401);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('returns a stable unavailable response when the binding fails', async () => {
    const service: WorkerService = {
      fetch: vi.fn(async () => {
        throw new Error('binding unavailable');
      }),
    };
    const response = await proxyApiRequest(
      new Request(requestUrl, {
        headers: { authorization: 'Bearer session-token' },
      }),
      { path: ['v1', 'conversations'] },
      { service, upstreamBaseUrl: 'https://edge.internal' },
    );

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      error: 'API service unavailable',
    });
  });
});
