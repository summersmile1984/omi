export function requestId(request: Request): string {
  return request.headers.get("x-request-id") || crypto.randomUUID();
}

export function withRequestId(response: Response, id: string): Response {
  const headers = new Headers(response.headers);
  headers.set("x-request-id", id);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}
