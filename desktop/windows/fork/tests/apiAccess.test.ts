import { expect, it, vi } from 'vitest'
import type { WebRequest } from 'electron'
import { installNativeApiAccess } from '../native/apiAccess'

it('only grants the current renderer its selected API origin, including preflight; auth and unrelated windows stay untouched', () => {
  const handlers: Record<string, (details: any, callback: any) => void> = {}
  const filters: unknown[] = []
  const request = Object.fromEntries(
    ['onBeforeSendHeaders', 'onHeadersReceived', 'onCompleted', 'onErrorOccurred'].map((name) => [
      name,
      (filter: unknown, handler: any) => {
        filters.push(filter)
        handlers[name] = handler
      }
    ])
  ) as unknown as WebRequest
  let contents = 'http://localhost:17717'
  installNativeApiAccess(
    request,
    'http://127.0.0.1:34810',
    () => 'http://localhost:17717',
    () => contents
  )
  expect(
    filters.every(
      (filter) => JSON.stringify(filter) === JSON.stringify({ urls: ['http://127.0.0.1:34810/*'] })
    )
  ).toBe(true)
  const base = {
    id: 1,
    url: 'http://127.0.0.1:34810/v1/action-items',
    webContentsId: 2,
    requestHeaders: { Origin: contents, Authorization: 'Bearer access-only' },
    method: 'OPTIONS',
    responseHeaders: { 'Access-Control-Allow-Origin': ['*'] }
  }
  const before = vi.fn(),
    after = vi.fn()
  handlers.onBeforeSendHeaders(base, before)
  expect(before.mock.calls[0][0].requestHeaders).toEqual({ Authorization: 'Bearer access-only' })
  handlers.onHeadersReceived(base, after)
  expect(after.mock.calls[0][0]).toMatchObject({
    statusLine: 'HTTP/1.1 200 OK',
    responseHeaders: {
      'access-control-allow-origin': [contents],
      'access-control-allow-headers': ['authorization, content-type']
    }
  })
  expect(after.mock.calls[0][0].responseHeaders['Access-Control-Allow-Origin']).toBeUndefined()
  handlers.onCompleted(base, undefined)
  for (const altered of [
    { url: 'https://api.omi.me/v2/apps' },
    { url: 'http://127.0.0.1:34810/api/auth/sign-in/email' },
    { requestHeaders: { Origin: 'https://untrusted.example.invalid' } }
  ]) {
    before.mockClear()
    after.mockClear()
    handlers.onBeforeSendHeaders({ ...base, ...altered }, before)
    handlers.onHeadersReceived({ ...base, ...altered }, after)
    expect(after).toHaveBeenCalledWith({})
  }
  contents = 'https://mail.google.com'
  handlers.onBeforeSendHeaders(base, before)
  after.mockClear()
  handlers.onHeadersReceived(base, after)
  expect(after).toHaveBeenCalledWith({})
})
