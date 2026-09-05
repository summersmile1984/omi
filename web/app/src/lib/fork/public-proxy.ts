import { moonshineJson } from '@tschk/moonshine-next/server';
import {
  controlledSharePayload,
  ShareRequestError,
  type ShareKind,
} from './share-client';

const PUBLIC_PATH_PATTERNS: RegExp[] = [
  /^v1\/approved-apps$/,
  /^v2\/apps$/,
  /^v1\/fair-use\/case\/[^/]+\/status$/,
  /^v1\/action-items\/shared\/[a-f0-9]{32}$/i,
  /^v2\/messages\/shared\/[a-f0-9]{32}$/i,
];

export function isPublicProxyPath(path: string): boolean {
  return PUBLIC_PATH_PATTERNS.some((pattern) => pattern.test(path));
}

const UPSTREAM_TIMEOUT_MS = 10_000;
const SUCCESS_CACHE_CONTROL = 'public, max-age=60, stale-while-revalidate=300';
const PRIVATE_PATH =
  /^(?:v1\/fair-use\/case\/[^/]+\/status|v1\/action-items\/shared\/[a-f0-9]{32}|v2\/messages\/shared\/[a-f0-9]{32})$/i;
type ApiBinding = { fetch(request: Request): Promise<Response> };

const TASK_SHARE_PATH = /^v1\/action-items\/shared\/[a-f0-9]{32}$/i;
const CHAT_SHARE_PATH = /^v2\/messages\/shared\/[a-f0-9]{32}$/i;

export function publicProxyCacheControl(
  path: string,
  succeeded: boolean,
): string | undefined {
  if (PRIVATE_PATH.test(path)) return 'private, no-store';
  return succeeded ? SUCCESS_CACHE_CONTROL : undefined;
}

function shareKind(path: string): ShareKind | null {
  if (TASK_SHARE_PATH.test(path)) return 'tasks';
  if (CHAT_SHARE_PATH.test(path)) return 'chat';
  return null;
}

export function isShareProxyRequest(request: Request): boolean {
  const path = new URL(request.url).pathname.slice('/api/proxy/public/'.length);
  return TASK_SHARE_PATH.test(path) || CHAT_SHARE_PATH.test(path);
}

export async function proxyPublicGet(
  request: Request,
  apiBinding?: ApiBinding,
): Promise<Response> {
  const requestUrl = new URL(request.url);
  const path = requestUrl.pathname.slice('/api/proxy/public/'.length);
  if (!isPublicProxyPath(path))
    return moonshineJson({ error: 'Not a public endpoint' }, { status: 404 });
  const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL;
  if (!apiBaseUrl)
    return moonshineJson({ error: 'Public API unavailable' }, { status: 503 });

  const search = requestUrl.searchParams.toString();
  try {
    const upstream = new Request(
      `${apiBaseUrl.replace(/\/+$/, '')}/${path}${search ? `?${search}` : ''}`,
      {
        headers: { Accept: 'application/json' },
        redirect: 'error',
        signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      },
    );
    const response = apiBinding
      ? await apiBinding.fetch(upstream)
      : await fetch(upstream);
    const kind = shareKind(path);
    if (kind && !response.ok) {
      const unavailable = response.status === 404 || response.status === 410;
      return moonshineJson(
        { error: unavailable ? 'Share unavailable' : 'Share lookup failed' },
        {
          status: response.status,
          headers: {
            'Cache-Control': 'private, no-store',
            'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
          },
        },
      );
    }
    let body = await response.text();
    if (kind) {
      try {
        body = JSON.stringify(controlledSharePayload(kind, JSON.parse(body)));
      } catch (error) {
        const unavailable =
          error instanceof ShareRequestError && error.code === 'unavailable';
        return moonshineJson(
          { error: unavailable ? 'Share unavailable' : 'Invalid share response' },
          {
            status: unavailable ? 404 : 502,
            headers: {
              'Cache-Control': 'private, no-store',
              'Referrer-Policy': 'no-referrer',
              'X-Content-Type-Options': 'nosniff',
            },
          },
        );
      }
    }
    const headers: Record<string, string> = {
      'Content-Type':
        response.headers.get('content-type') || 'application/json; charset=utf-8',
      'Referrer-Policy': 'no-referrer',
      'X-Content-Type-Options': 'nosniff',
    };
    const cacheControl = publicProxyCacheControl(path, response.ok);
    if (cacheControl) headers['Cache-Control'] = cacheControl;
    return new Response(body, { status: response.status, headers });
  } catch {
    return moonshineJson({ error: 'Proxy request failed' }, { status: 502 });
  }
}
