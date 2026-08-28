const COOKIE_SESSION_RESPONSES = new Set(['sign-in/email', 'sign-up/email']);

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
