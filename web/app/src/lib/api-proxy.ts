export type WorkerService = {
  fetch(request: Request): Promise<Response>;
};

type ProxyOptions = {
  service?: WorkerService;
  upstreamBaseUrl: string;
  fetchImpl?: typeof fetch;
};

const REQUEST_HEADERS = [
  'accept',
  'accept-language',
  'authorization',
  'cache-control',
  'content-type',
  'if-match',
  'if-modified-since',
  'if-none-match',
  'if-unmodified-since',
  'last-event-id',
  'pragma',
  'range',
  'x-app-build',
  'x-app-platform',
  'x-app-version',
  'x-device-id-hash',
  'x-request-id',
] as const;

const HOP_BY_HOP_RESPONSE_HEADERS = [
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
] as const;

function forwardedRequestHeaders(request: Request): Headers {
  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  return headers;
}

function forwardedResponseHeaders(response: Response, path: string): Headers {
  const headers = new Headers(response.headers);
  for (const name of HOP_BY_HOP_RESPONSE_HEADERS) headers.delete(name);
  if (
    path.includes('app-categories') ||
    path.includes('app-capabilities') ||
    path.includes('app/plans')
  ) {
    headers.set('cache-control', 'public, max-age=3600, stale-while-revalidate=86400');
  }
  return headers;
}

export async function proxyApiRequest(
  request: Request,
  params: { path: string[] },
  options: ProxyOptions,
): Promise<Response> {
  const authorization = request.headers.get('authorization');
  if (!authorization) {
    return Response.json({ error: 'Authorization header required' }, { status: 401 });
  }

  const path = params.path.join('/');
  const incomingUrl = new URL(request.url);
  const target = new URL(`/${path}`, options.upstreamBaseUrl);
  target.search = incomingUrl.search;
  const upstreamRequest = new Request(target, {
    method: request.method,
    headers: forwardedRequestHeaders(request),
    body:
      request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
    redirect: 'manual',
  });

  try {
    const upstream = options.service
      ? await options.service.fetch(upstreamRequest)
      : await (options.fetchImpl || fetch)(upstreamRequest);
    return new Response(upstream.status === 204 ? null : upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: forwardedResponseHeaders(upstream, path),
    });
  } catch {
    return Response.json({ error: 'API service unavailable' }, { status: 503 });
  }
}
