import { afterEach, describe, expect, it, vi } from 'vitest';
import { getRealtimeTicket } from './realtimeTicket';

describe('getRealtimeTicket', () => {
  afterEach(() => vi.restoreAllMocks());

  it('uses the same-origin httpOnly session to request a short-lived ticket', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(Response.json({ ticket: 'signed.ticket' }));

    await expect(getRealtimeTicket()).resolves.toBe('signed.ticket');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/proxy/v1/realtime/web-ticket',
      expect.objectContaining({ method: 'POST', credentials: 'include' }),
    );
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).has('authorization')).toBe(
      false,
    );
  });

  it('does not accept a missing ticket', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({}));
    await expect(getRealtimeTicket()).rejects.toThrow('invalid ticket');
  });
});
