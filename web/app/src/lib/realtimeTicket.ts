type RealtimeTicketResponse = {
  ticket?: unknown;
};

export async function getRealtimeTicket(): Promise<string> {
  const response = await fetch('/api/proxy/v1/realtime/web-ticket', {
    method: 'POST',
    credentials: 'include',
    headers: { 'content-type': 'application/json' },
    body: '{}',
  });
  if (!response.ok) {
    throw new Error(
      response.status === 401 ? 'Not authenticated' : 'Realtime service unavailable',
    );
  }
  const body = (await response.json()) as RealtimeTicketResponse;
  if (
    typeof body.ticket !== 'string' ||
    body.ticket.length < 1 ||
    body.ticket.length > 32_768
  ) {
    throw new Error('Realtime service returned an invalid ticket');
  }
  return body.ticket;
}
