import { request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'

// Native main-process transport. Node's web fetch synthesizes Sec-Fetch-Mode
// without a browser Origin; Better Auth correctly rejects that browser-shaped
// request. A native HTTP request has neither browser cookies nor Fetch Metadata.
// Do not forge an Origin or weaken the server's browser CSRF checks.
export const nativeIdentityFetch: typeof fetch = async (
  input: RequestInfo | URL,
  init: RequestInit = {}
) => {
  const url = new URL(String(input))
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username ||
    url.password ||
    init.redirect !== 'error'
  ) {
    throw new Error('Invalid native identity request')
  }
  return new Promise<Response>((resolve, reject) => {
    const request = (url.protocol === 'https:' ? httpsRequest : httpRequest)(
      url,
      {
        method: init.method,
        signal: init.signal ?? undefined,
        headers: Object.fromEntries(new Headers(init.headers).entries())
      },
      (response) => {
        const status = response.statusCode ?? 500
        if (status >= 300 && status < 400) {
          response.resume()
          reject(new Error('Identity redirects are not allowed'))
          return
        }
        const chunks: Buffer[] = []
        let size = 0
        response.on('data', (chunk: Buffer) => {
          size += chunk.length
          if (size > 1024 * 1024) {
            response.destroy(new Error('Identity response exceeds limit'))
            return
          }
          chunks.push(chunk)
        })
        response.on('error', reject)
        response.on('end', () => {
          const headers = new Headers()
          for (let i = 0; i < response.rawHeaders.length; i += 2)
            headers.append(response.rawHeaders[i], response.rawHeaders[i + 1])
          resolve(
            new Response([204, 205, 304].includes(status) ? null : Buffer.concat(chunks), {
              status,
              headers
            })
          )
        })
      }
    )
    request.on('error', reject)
    if (init.body !== undefined && init.body !== null) {
      if (typeof init.body !== 'string') {
        request.destroy()
        reject(new Error('Invalid identity body'))
        return
      }
      request.end(init.body)
      return
    }
    request.end()
  })
}
