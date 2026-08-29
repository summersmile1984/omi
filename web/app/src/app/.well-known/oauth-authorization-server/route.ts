import { NextRequest } from 'next/server';
import { env } from 'cloudflare:workers';
import { proxyBetterAuthMetadata } from '@/lib/auth-proxy';

const AUTH_SERVER_URL = process.env.NEXT_PUBLIC_AUTH_SERVER_URL;

export async function GET(request: NextRequest): Promise<Response> {
  const authService = (env as unknown as { AUTH?: { fetch: typeof fetch } }).AUTH;
  return proxyBetterAuthMetadata(request, authService, AUTH_SERVER_URL);
}

export const HEAD = GET;
