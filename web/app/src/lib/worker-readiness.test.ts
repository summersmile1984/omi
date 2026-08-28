import { describe, expect, it, vi } from 'vitest';

import { workerReadiness } from './worker-readiness';

describe('workerReadiness', () => {
  it('checks Edge through the service binding', async () => {
    const fetch = vi.fn(async (request: Request) => {
      expect(request.url).toBe('https://edge.internal/ready');
      return Response.json({ status: 'ready', service: 'edge' });
    });
    const response = await workerReadiness({ fetch });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: 'ready', service: 'edge' });
  });

  it('fails closed when the binding is absent or unavailable', async () => {
    expect((await workerReadiness(undefined)).status).toBe(503);
    const response = await workerReadiness({
      fetch: vi.fn(async () => {
        throw new Error('binding unavailable');
      }),
    });
    expect(response.status).toBe(503);
  });
});
