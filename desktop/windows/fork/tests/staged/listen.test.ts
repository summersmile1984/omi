import { expect, it, vi } from 'vitest'
import { EventEmitter } from 'node:events'
const f = vi.hoisted(() => ({
  handlers: new Map<string, any>(),
  owner: 'lease-a',
  minted: true,
  session: vi.fn(async () => ({ token: 'main-owned-jwt', apiBase: '', desktopApiBase: '' })),
  sockets: [] as { url: string; options: { headers: Record<string, string> }; ws: any }[]
}))
vi.mock('../../native/runtime', () => ({
  identityOwner: { snapshot: () => ({ owner: f.owner }), ownsAccessToken: () => f.minted },
  currentBackendSession: f.session
}))
vi.mock('../../../src/main/agentKernel/byokStore', () => ({
  ByokKeyStore: class {
    getAllKeys() {
      return {}
    }
  }
}))
vi.mock('electron', () => ({
  ipcMain: { handle: (name: string, fn: any) => f.handlers.set(name, fn), on: vi.fn() },
  webContents: { fromId: vi.fn() }
}))
vi.mock('ws', async () => {
  const { EventEmitter } = await import('node:events')
  class Socket extends EventEmitter {
    static OPEN = 1
    readyState = 0
    constructor(url: string, options: any) {
      super()
      f.sockets.push({ url, options, ws: this })
    }
    close() {
      this.readyState = 3
      this.emit('close', 1000, Buffer.alloc(0))
    }
    terminate() {
      this.close()
    }
    send() {}
  }
  return { default: Socket }
})
it('uses profile native-header WS endpoints and main-minted tokens; old owner/unauthorized window cannot start', async () => {
  const listen = await import('../../../src/main/ipc/omiListen')
  const { profile } = await import('../../native/profile.generated')
  let allowed = true
  listen.registerOmiListenHandlers(() => allowed)
  const sender = Object.assign(new EventEmitter(), {
    id: 7,
    isDestroyed: () => false,
    send: vi.fn()
  })
  const event = { sender }
  const args = {
    sessionId: 'proof',
    source: 'mic',
    token: 'renderer-token',
    deviceIdHash: 'abc12345',
    language: 'en',
    mode: 'ptt'
  }
  await f.handlers.get('omi-listen:start')(event, args)
  expect(f.sockets).toHaveLength(1)
  expect(f.sockets[0].url).toBe(
    profile.apiBase.replace(/^http/, 'ws') +
      '/v2/voice-message/transcribe-stream?language=en&sample_rate=16000&codec=linear16&channels=1'
  )
  expect(f.sockets[0].options.headers.Authorization).toBe('Bearer main-owned-jwt')
  expect(listen.buildListenEndpoint('conversation', 'en')).toContain(
    '/v4/listen?language=en&sample_rate=16000&codec=pcm16'
  )
  listen.killSessionsForOwner(7)
  f.minted = false
  await expect(f.handlers.get('omi-listen:start')(event, args)).rejects.toThrow('Departed identity')
  f.minted = true
  allowed = false
  await expect(f.handlers.get('omi-listen:start')(event, args)).rejects.toThrow('not allowed')
  allowed = true
  let finish!: (value: any) => void
  f.session.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve
      })
  )
  const pending = f.handlers.get('omi-listen:start')(event, args)
  f.owner = 'lease-b'
  finish({ token: 'old-owner-jwt' })
  await expect(pending).rejects.toThrow('Departed identity')
  expect(f.sockets).toHaveLength(1)
})
