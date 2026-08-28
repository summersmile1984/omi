import { NextRequest, NextResponse } from 'next/server';
import { env } from 'cloudflare:workers';
import {
  betterAuthRequestHeaders,
  betterAuthResponseHeaders,
  betterAuthTarget,
  sanitizeBetterAuthResponse,
} from '@/lib/auth-proxy';

const AUTH_SERVER_URL = process.env.NEXT_PUBLIC_AUTH_SERVER_URL;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyAuthRequest(request, await params);
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyAuthRequest(request, await params);
}

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyAuthRequest(request, await params);
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyAuthRequest(request, await params);
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyAuthRequest(request, await params);
}

async function proxyAuthRequest(
  request: NextRequest,
  params: { path: string[] },
): Promise<Response> {
  const authService = (env as unknown as { AUTH?: { fetch: typeof fetch } }).AUTH;
  if (!authService && !AUTH_SERVER_URL) {
    return NextResponse.json({ error: 'auth proxy is not configured' }, { status: 503 });
  }

  const authPath = params.path.join('/');
  const target = betterAuthTarget(authPath, request.nextUrl.search, AUTH_SERVER_URL);
  const headers = betterAuthRequestHeaders(request.headers);

  try {
    const upstreamRequest = new Request(target, {
      method: request.method,
      headers,
      body:
        request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
    });
    // Worker-to-Worker requests on workers.dev can return Cloudflare's 1042
    // synthetic error. Use a service binding in Cloudflare and retain direct
    // fetch as a local-development fallback.
    const response = authService
      ? await authService.fetch(upstreamRequest)
      : await fetch(upstreamRequest);
    const body = sanitizeBetterAuthResponse(authPath, response, await response.text());
    if (body === null) {
      return NextResponse.json({ error: 'invalid auth response' }, { status: 502 });
    }
    return new NextResponse(body, {
      status: response.status,
      headers: betterAuthResponseHeaders(response),
    });
  } catch {
    return NextResponse.json({ error: 'auth service unavailable' }, { status: 503 });
  }
}
