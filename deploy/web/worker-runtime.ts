// This module replaces only the Bun deployment adapter during the Worker build.
// The original generated app keeps ownership of SSR, routes and response headers.
let handler: ((request: Request) => Promise<Response>) | undefined;

export function createBunServer(options: { fetch: typeof handler }) {
  if (handler || !options.fetch)
    throw new Error('Worker fetch owner must be registered once');
  handler = options.fetch;
  return { url: new URL('https://web.build.invalid'), port: 0, stop: async () => {} };
}

export function fetchApp(request: Request) {
  if (!handler) throw new Error('Worker fetch owner not registered');
  return handler(request);
}

// Deployment readiness belongs to the target adapter, outside application
// routing. Prove the actual Web -> Edge binding; never certify SSR alone.
export async function workerReadiness(
  request: Request,
  edge?: { fetch(request: Request): Promise<Response> },
): Promise<Response | undefined> {
  if (new URL(request.url).pathname !== '/api/worker-ready') return undefined;
  const headers = { 'Cache-Control': 'no-store' };
  if (request.method !== 'GET')
    return new Response(null, { status: 405, headers: { ...headers, Allow: 'GET' } });
  try {
    if (edge) {
      const response = await edge.fetch(new Request('https://edge.internal/ready'));
      const body = await response.json() as { status?: string };
      if (response.status === 200 && body?.status === 'ready')
        return Response.json({ status: 'ready' }, { headers });
    }
  } catch {
    // The public probe exposes no dependency payload, exceptions or credentials.
  }
  return Response.json({ status: 'unavailable' }, { status: 503, headers });
}
