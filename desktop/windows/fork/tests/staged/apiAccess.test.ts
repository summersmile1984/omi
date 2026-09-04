// @vitest-environment jsdom
import { expect, it, vi } from 'vitest'
import type { WebRequest } from 'electron'
import { BYOK_PROVIDERS } from '../../../src/shared/byok'
import { installNativeApiAccess } from '../../native/apiAccess'
vi.mock('../../renderer/identity', () => ({
  auth: { currentUser: { uid: 'synthetic', owner: 'lease', getIdToken: async () => 'access' } }
}))
vi.mock('../../../src/renderer/src/lib/clientDevice', () => ({
  getWindowsDeviceIdHash: async () => 'synthetic-device-hash'
}))
it('admits the actual staged axios platform/device/BYOK header set to the native API boundary', async () => {
  window.omi = {
    byokGetAll: async () => Object.fromEntries(BYOK_PROVIDERS.map((p) => [p, 'synthetic-key'])),
    byokValidatedProviders: async () => []
  } as unknown as typeof window.omi
  const { refreshByokKeys } = await import('../../../src/renderer/src/lib/byokKeys')
  await refreshByokKeys()
  const { omiApi, setAppVersion } = await import('../../../src/renderer/src/lib/apiClient')
  setAppVersion('1.0.0-test')
  let names: string[] = []
  await omiApi.patch(
    '/v1/users/language',
    { language: 'en' },
    {
      adapter: async (config) => {
        names = Object.keys(config.headers.toJSON()).filter(
          (name) => name.toLowerCase() !== 'accept'
        )
        return { config, status: 200, statusText: 'OK', headers: {}, data: {} }
      }
    }
  )
  expect(names).toContain('X-App-Platform')
  expect(names).toContain('X-App-Version')
  expect(names).toContain('X-Device-Id-Hash')
  const handlers: Record<string, (details: any, callback: any) => void> = {}
  const request = Object.fromEntries(
    ['onBeforeSendHeaders', 'onHeadersReceived', 'onCompleted', 'onErrorOccurred'].map((name) => [
      name,
      (_filter: unknown, handler: any) => {
        handlers[name] = handler
      }
    ])
  ) as unknown as WebRequest
  const origin = 'http://localhost:17717'
  installNativeApiAccess(
    request,
    'http://127.0.0.1:34810',
    () => origin,
    () => origin
  )
  const details = {
    id: 5,
    url: 'http://127.0.0.1:34810/v1/users/language',
    webContentsId: 2,
    method: 'OPTIONS',
    requestHeaders: { Origin: origin, 'Access-Control-Request-Headers': names.join(', ') },
    responseHeaders: {}
  }
  const before = vi.fn(),
    after = vi.fn()
  handlers.onBeforeSendHeaders(details, before)
  expect(before.mock.calls[0][0].requestHeaders.Origin).toBeUndefined()
  handlers.onHeadersReceived(details, after)
  const allowed =
    after.mock.calls[0][0].responseHeaders['access-control-allow-headers'][0].split(', ')
  expect(names.every((name) => allowed.includes(name.toLowerCase()))).toBe(true)
  expect(allowed).not.toContain('*')
})
