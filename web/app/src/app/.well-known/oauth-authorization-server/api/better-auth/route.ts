import { NextRequest, NextResponse } from 'next/server';
import { env } from 'cloudflare:workers';
import {
  betterAuthRequestHeaders,
  betterAuthResponseHeaders,
  betterAuthTarget,
} from '@/lib/auth-proxy';

const AUTH_SERVER_URL = process.env.NEXT_PUBLIC_AUTH_SERVER_URL;

export async function GET(request: NextRequest): Promise<Response> {
  const authService = (env as unknown as { AUTH?: { fetch: typeof fetch } }).AUTH;
  if (!authService && !AUTH_SERVER_URL) {
    return NextResponse.json({ error: 'auth proxy is not configured' }, { status: 503 });
  }
  const target = betterAuthTarget(
    '.well-known/oauth-authorization-server',
    request.nextUrl.search,
    AUTH_SERVER_URL,
  );
  try {
    const upstream = new Request(target, {
      headers: betterAuthRequestHeaders(request.headers),
    });
    const response = authService
      ? await authService.fetch(upstream)
      : await fetch(upstream);
    return new NextResponse(await response.text(), {
      status: response.status,
      headers: betterAuthResponseHeaders(response),
    });
  } catch {
    return NextResponse.json({ error: 'auth service unavailable' }, { status: 503 });
  }
}
