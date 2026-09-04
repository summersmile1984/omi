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
