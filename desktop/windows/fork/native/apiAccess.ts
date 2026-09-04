import type { WebRequest } from 'electron'
import { BYOK_HEADER_NAMES } from '../../src/shared/byok'

// The production axios owner adds platform/version/device identity to every
// request. BYOK names come from the same shared contract as that owner.
const allowedHeaders = [
  'authorization',
  'content-type',
  'x-app-platform',
  'x-app-version',
  'x-device-id-hash',
  ...Object.values(BYOK_HEADER_NAMES).map((name) => name.toLowerCase())
]

type RequestContext = { id: number; url: string; webContentsId?: number }
type ContentsOrigin = (id: number) => string | null

// Reuse Electron's existing native API CORS owner, narrowed to this deployment
// and this application's renderer. This never handles the identity endpoints.
export function installNativeApiAccess(
  request: WebRequest,
  apiBase: string,
  rendererOrigin: () => string | null,
  contentsOrigin: ContentsOrigin
): void {
  const apiOrigin = new URL(apiBase).origin
  const admitted = new Map<number, string>()
  const filter = { urls: [apiOrigin + '/*'] }
  const originFor = (details: RequestContext): string | null => {
    try {
      const url = new URL(details.url)
      const origin = rendererOrigin()
      return origin &&
        url.origin === apiOrigin &&
        !/^\/api\/auth(?:\/|$)/.test(url.pathname) &&
        details.webContentsId !== undefined &&
        contentsOrigin(details.webContentsId) === origin
        ? origin
        : null
    } catch {
      return null
    }
  }
  request.onBeforeSendHeaders(filter, (details, callback) => {
    const origin = originFor(details)
    const headers = { ...details.requestHeaders }
    const key = Object.keys(headers).find((key) => key.toLowerCase() === 'origin')
    const preflightKey = Object.keys(headers).find(
      (key) => key.toLowerCase() === 'access-control-request-headers'
    )
    const requested = preflightKey ? String(headers[preflightKey]).split(',') : []
    const knownHeaders = requested.every((name) =>
      allowedHeaders.includes(name.trim().toLowerCase())
    )
    if (origin && key && headers[key] === origin && knownHeaders) {
      delete headers[key]
      admitted.set(details.id, origin)
    }
    callback({ requestHeaders: headers })
  })
  request.onHeadersReceived(filter, (details, callback) => {
    const origin = admitted.get(details.id)
    if (!origin || originFor(details) !== origin) {
      callback({})
      return
    }
    const responseHeaders = { ...details.responseHeaders }
    for (const key of Object.keys(responseHeaders)) {
      if (/^access-control-allow-/i.test(key)) delete responseHeaders[key]
    }
    Object.assign(responseHeaders, {
      'access-control-allow-origin': [origin],
      'access-control-allow-headers': [allowedHeaders.join(', ')],
      'access-control-allow-methods': ['GET, POST, PUT, PATCH, DELETE, OPTIONS']
    })
    callback({
      responseHeaders,
      ...(details.method === 'OPTIONS' ? { statusLine: 'HTTP/1.1 200 OK' } : {})
    })
  })
  request.onCompleted(filter, (details) => {
    admitted.delete(details.id)
  })
  request.onErrorOccurred(filter, (details) => {
    admitted.delete(details.id)
  })
}
