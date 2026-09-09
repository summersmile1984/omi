import {
  app,
  BrowserWindow,
  ipcMain,
  safeStorage,
  session,
  type IpcMainInvokeEvent
} from 'electron'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { IdentityOwner } from './owner'
import { IdentityClient } from './client'
import { nativeIdentityFetch } from './http'
import { SecureCredentialStore } from './store'
import { IdentityError, type IdentityResult } from './contract'
import { profile } from './profile.generated'
import { killSessionsForOwner } from '../../src/main/ipc/omiListen'
import { rendererBaseUrl } from '../../src/main/rendererServer'
import { wipeUserData } from '../../src/main/ipc/db'
import { ByokKeyStore } from '../../src/main/agentKernel/byokStore'
import { McpKeyStore } from '../../src/main/mcp/mcpKeyStore'
import { configureAiProfileSession } from '../../src/main/assistants/aiUserProfile/service'
import { configurePiMonoSession } from '../../src/main/codingAgent/piMonoSession'
import { configureRewindEmbedSession } from '../../src/main/rewind/embeddingService'
import {
  ensurePiMonoAdapterRegistered,
  setControlPlaneOwner,
  controlPlaneOwnerId
} from '../../src/main/agentKernel/controlPlane'
import {
  clearRendererConversationBinding,
  fenceRendererConversationOwner
} from '../../src/main/jit/rendererConversationBinding'

export const identityOwner = new IdentityOwner(
  new IdentityClient(profile.authBase, nativeIdentityFetch),
  new SecureCredentialStore(
    join(app.getPath('userData'), 'identity.bin'),
    profile.applicationId,
    safeStorage,
    process.platform
  ),
  async () => {
    await wipeUserData()
    // db.wipe's best-effort clears are insufficient for admitting a NEW owner.
    // Repeat the actual stores fail-closed inside the same owner mutation queue.
    const byok = new ByokKeyStore()
    byok.clearAll()
    byok.clearCodexKey()
    new McpKeyStore().clearAll()
    const gmail = session.fromPartition('persist:omi-gmail')
    await gmail.clearStorageData({
      storages: ['cookies', 'localstorage', 'indexdb', 'serviceworkers']
    })
    await gmail.clearCache()
  }
)

export async function currentBackendSession(
  force = true
): Promise<{ token: string; apiBase: string; desktopApiBase: string } | null> {
  const snapshot = identityOwner.snapshot()
  if (!snapshot.owner) return null
  const token = await identityOwner.token(snapshot.owner, force)
  if (identityOwner.snapshot().owner !== snapshot.owner) throw new IdentityError('superseded')
  return { token, apiBase: profile.apiBase, desktopApiBase: profile.apiBase }
}

export function trustedIdentitySender(event: IpcMainInvokeEvent): boolean {
  if (event.senderFrame !== event.sender.mainFrame) return false
  if (!BrowserWindow.fromWebContents(event.sender)) return false
  try {
    const url = new URL(event.senderFrame.url)
    const origin = rendererBaseUrl() ?? process.env.ELECTRON_RENDERER_URL
    if (origin && url.origin === new URL(origin).origin) return true
    const root = pathToFileURL(join(app.getAppPath(), 'out/renderer/')).href
    return url.protocol === 'file:' && url.href.startsWith(root)
  } catch {
    // Companion windows can still be uninitialized during cold restoration.
    // An absent/malformed URL is an untrusted sender, not an identity failure.
    return false
  }
}

let installed = false
export function registerIdentity(): void {
  if (installed) throw new Error('Identity already registered')
  installed = true
  let lastOwner: string | null | undefined
  identityOwner.subscribe((snapshot, token) => {
    if (snapshot.owner !== lastOwner) {
      lastOwner = snapshot.owner
      for (const window of BrowserWindow.getAllWindows())
        killSessionsForOwner(window.webContents.id)
      const session =
        snapshot.user && token
          ? { token, apiBase: profile.apiBase, desktopApiBase: profile.apiBase }
          : null
      // Derive every main-side owner and address here, never from renderer data.
      configureAiProfileSession(session)
      configurePiMonoSession(session)
      configureRewindEmbedSession(session)
      setControlPlaneOwner(snapshot.user?.id ?? null)
      if (snapshot.user) fenceRendererConversationOwner(controlPlaneOwnerId())
      else clearRendererConversationBinding()
      ensurePiMonoAdapterRegistered()
    }
    for (const window of BrowserWindow.getAllWindows()) {
      if (
        !window.isDestroyed() &&
        trustedIdentitySender({
          sender: window.webContents,
          senderFrame: window.webContents.mainFrame
        } as IpcMainInvokeEvent)
      )
        window.webContents.send('identity:changed', snapshot)
    }
  })
  function handle<T>(channel: string, callback: (...args: never[]) => Promise<T>): void {
    ipcMain.handle(channel, async (event, ...args): Promise<IdentityResult<T>> => {
      try {
        if (!trustedIdentitySender(event)) throw new IdentityError('invalid_input')
        return { ok: true, value: await callback(...(args as never[])) }
      } catch (error) {
        return { ok: false, code: error instanceof IdentityError ? error.code : 'unavailable' }
      }
    })
  }
  handle('identity:snapshot', async () => {
    await identityOwner.initialize()
    return identityOwner.snapshot()
  })
  handle('identity:authenticate', (input) => identityOwner.authenticate(input))
  handle('identity:token', (owner, force) => identityOwner.token(owner, force))
  handle('identity:name', (owner, name) => identityOwner.updateName(owner, name))
  handle('identity:signOut', (owner) => identityOwner.signOut(owner))
  handle('identity:invalidate', (owner) => identityOwner.invalidate(owner))
}
