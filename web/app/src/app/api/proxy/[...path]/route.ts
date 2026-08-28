import { env } from 'cloudflare:workers';
import { NextRequest } from 'next/server';

import { proxyApiRequest, type WorkerService } from '@/lib/api-proxy';

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || 'https://api.omi.me';

type RouteContext = { params: Promise<{ path: string[] }> };

function edgeService(): WorkerService | undefined {
  return (env as unknown as { EDGE?: WorkerService }).EDGE;
}

async function handle(request: NextRequest, context: RouteContext) {
  return proxyApiRequest(request, await context.params, {
    service: edgeService(),
    upstreamBaseUrl: API_BASE_URL,
  });
}

export const GET = handle;
export const HEAD = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
