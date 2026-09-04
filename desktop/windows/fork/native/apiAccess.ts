import type { WebRequest } from 'electron'

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
    if (origin && key && headers[key] === origin) {
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
      'access-control-allow-headers': ['authorization, content-type'],
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
