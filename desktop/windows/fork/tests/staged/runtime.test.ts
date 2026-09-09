import { expect, it, vi } from 'vitest'
const f = vi.hoisted(() => {
  const frame = { url: 'http://localhost:17717/index.html' }
  const blank = { url: '' }
  const external = { url: 'https://mail.google.com/' }
  const windows = [frame, blank, external].map((mainFrame, id) => ({
    isDestroyed: () => false,
    webContents: { id, mainFrame, send: vi.fn() }
  }))
  return {
    windows,
    frame,
    handlers: new Map<string, (...args: any[]) => Promise<any>>(),
    stored: {
      credential: {
        session: 'opaque-restored',
        user: { id: 'a', email: 'a@example.invalid', name: 'A' }
      },
      lastUserId: 'a',
      clearEpoch: 0,
      cleanupPending: false
    },
    clear: vi.fn(async () => {}),
    bind: vi.fn(),
    owner: vi.fn(),
    kill: vi.fn(),
    http: vi.fn()
  }
})
vi.mock('electron', () => ({
  app: { getPath: () => '/isolated-fixture', getAppPath: () => '/isolated-stage' },
  safeStorage: {},
  session: { fromPartition: () => ({ clearStorageData: f.clear, clearCache: f.clear }) },
  BrowserWindow: {
    fromWebContents: (wc: any) => f.windows.find((w) => w.webContents === wc),
    getAllWindows: () => f.windows
  },
  ipcMain: { handle: (name: string, handler: any) => f.handlers.set(name, handler) }
}))
vi.mock('../../native/store', () => ({
  SecureCredentialStore: class {
    read() {
      return structuredClone(f.stored)
    }
    write(value: typeof f.stored) {
      f.stored = structuredClone(value)
    }
  }
}))
vi.mock('../../native/http', () => ({ nativeIdentityFetch: f.http }))
vi.mock('../../../src/main/ipc/omiListen', () => ({ killSessionsForOwner: f.kill }))
vi.mock('../../../src/main/rendererServer', () => ({
  rendererBaseUrl: () => 'http://localhost:17717'
}))
vi.mock('../../../src/main/ipc/db', () => ({ wipeUserData: f.clear }))
vi.mock('../../../src/main/agentKernel/byokStore', () => ({
  ByokKeyStore: class {
    clearAll = f.clear
    clearCodexKey = f.clear
  }
}))
vi.mock('../../../src/main/mcp/mcpKeyStore', () => ({
  McpKeyStore: class {
    clearAll = f.clear
  }
}))
vi.mock('../../../src/main/assistants/aiUserProfile/service', () => ({
  configureAiProfileSession: f.bind
}))
vi.mock('../../../src/main/codingAgent/piMonoSession', () => ({ configurePiMonoSession: f.bind }))
vi.mock('../../../src/main/rewind/embeddingService', () => ({
  configureRewindEmbedSession: f.bind
}))
vi.mock('../../../src/main/agentKernel/controlPlane', () => ({
  ensurePiMonoAdapterRegistered: vi.fn(),
  setControlPlaneOwner: f.owner,
  controlPlaneOwnerId: () => 'a'
}))
vi.mock('../../../src/main/jit/rendererConversationBinding', () => ({
  clearRendererConversationBinding: vi.fn(),
  fenceRendererConversationOwner: vi.fn()
}))

it('cold restore ignores still-blank companion URLs, binds owners in main, and never publishes opaque credentials to any renderer', async () => {
  const now = Math.floor(Date.now() / 1000)
  const token = [
    'header',
    Buffer.from(
      JSON.stringify({ sub: 'a', uid: 'a', sid: 'sid-a', iat: now, exp: now + 3600 })
    ).toString('base64url'),
    'signature'
  ].join('.')
  f.http.mockImplementation(async (url, init) => {
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer opaque-restored')
    return Response.json(
      String(url).endsWith('/token')
        ? { token }
        : String(url).endsWith('/sign-out')
          ? { success: true }
          : { user: f.stored.credential.user }
    )
  })
  const runtime = await import('../../native/runtime')
  runtime.registerIdentity()
  const event = { sender: f.windows[0].webContents, senderFrame: f.frame }
  const snapshot = await f.handlers.get('identity:snapshot')!(event)
  expect(snapshot).toMatchObject({ ok: true, value: { user: { id: 'a' }, problem: null } })
  expect(f.bind).toHaveBeenCalledTimes(3)
  expect(f.owner).toHaveBeenCalledWith('a')
  expect(f.windows[0].webContents.send).toHaveBeenCalled()
  expect(f.windows[1].webContents.send).not.toHaveBeenCalled()
  expect(f.windows[2].webContents.send).not.toHaveBeenCalled()
  expect(JSON.stringify(f.windows[0].webContents.send.mock.calls)).not.toContain('opaque-restored')
  expect(
    await f.handlers.get('identity:snapshot')!({
      sender: f.windows[2].webContents,
      senderFrame: f.windows[2].webContents.mainFrame
    })
  ).toEqual({ ok: false, code: 'invalid_input' })
  expect(
    await f.handlers.get('identity:snapshot')!({ ...event, senderFrame: { url: f.frame.url } })
  ).toEqual({ ok: false, code: 'invalid_input' })
  expect(await f.handlers.get('identity:token')!(event, 'forged-owner')).toEqual({
    ok: false,
    code: 'session_expired'
  })
  expect(runtime.identityOwner.ownsAccessToken('renderer-forged-jwt')).toBe(false)
  expect(runtime.identityOwner.ownsAccessToken(token)).toBe(true)
  const session = await runtime.currentBackendSession(true)
  expect(session?.token).toBe(token)
  expect(f.bind).toHaveBeenCalledTimes(3)
  await f.handlers.get('identity:signOut')!(event, snapshot.value.owner)
  expect(runtime.identityOwner.ownsAccessToken(token)).toBe(false)
  expect(runtime.identityOwner.snapshot().user).toBeNull()
  expect(f.clear).toHaveBeenCalled()
  expect([...f.handlers.keys()]).toEqual([
    'identity:snapshot',
    'identity:authenticate',
    'identity:token',
    'identity:name',
    'identity:signOut',
    'identity:invalidate'
  ])
})
