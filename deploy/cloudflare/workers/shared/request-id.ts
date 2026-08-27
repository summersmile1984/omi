export function requestId(request: Request): string {
  return request.headers.get("x-request-id") || crypto.randomUUID();
}

export function withRequestId(response: Response, id: string): Response {
  const headers = new Headers(response.headers);
  headers.set("x-request-id", id);
  if (response.status === 101 && response.webSocket) {
    return new Response(null, {
      status: 101,
      statusText: response.statusText,
      headers,
      webSocket: response.webSocket,
    });
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}
