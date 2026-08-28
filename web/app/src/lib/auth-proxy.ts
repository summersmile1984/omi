const COOKIE_SESSION_RESPONSES = new Set(['sign-in/email', 'sign-up/email']);

export function betterAuthTarget(
  authPath: string,
  search: string,
  authServerUrl?: string,
): URL {
  const target = new URL(
    `/api/better-auth/${authPath}`,
    authServerUrl || 'https://auth.internal',
  );
  target.search = search;
  return target;
}

export function betterAuthResponseHeaders(response: Response): Headers {
  const headers = new Headers();
  for (const name of ['content-type', 'cache-control', 'location', 'pragma', 'vary']) {
    const value = response.headers.get(name);
    if (value) headers.set(name, value);
  }
  const setCookies = response.headers.getSetCookie?.() ?? [];
  for (const value of setCookies) headers.append('set-cookie', value);
  headers.set('cache-control', 'no-store');
  return headers;
}

export function betterAuthRequestHeaders(requestHeaders: Headers): Headers {
  const headers = new Headers();
  for (const name of [
    'accept',
    'authorization',
    'cf-connecting-ip',
    'content-type',
    'cookie',
    'origin',
    'user-agent',
  ]) {
    const value = requestHeaders.get(name);
    if (value) headers.set(name, value);
  }
  return headers;
}

export function sanitizeBetterAuthResponse(
  authPath: string,
  response: Response,
  body: string,
): string | null {
  if (
    !response.ok ||
    !COOKIE_SESSION_RESPONSES.has(authPath) ||
    !response.headers.get('content-type')?.includes('application/json')
  ) {
    return body;
  }
  try {
    const value = JSON.parse(body) as Record<string, unknown>;
    delete value.token;
    return JSON.stringify(value);
  } catch {
    return null;
  }
}
