// Electron wiring for the backend-mediated provider sign-in flow. The actual
// flow (loopback listener, authorize URL, token exchange) lives in
// src/main/auth/signInFlow.ts and is Electron-free; this file supplies
// the browser opener, file logging, and window surfacing.
import { app, ipcMain, shell } from 'electron'
import { appendFileSync } from 'fs'
import { join } from 'path'
import { startSignIn } from '../auth/signInFlow'
import { AuthTokenStore } from './authStore'
import { BetterAuthClient, BetterAuthError, type BetterAuthStoredSession } from '../auth/betterAuthClient'
import { resolveWindowsDeployment } from '../../shared/deploymentProfile'
import type {
  BetterAuthResult,
  BetterAuthSignInRequest,
  SignInProvider,
  SignInResult
} from '../../shared/types'

// Main-process console.log only reaches the dev-server terminal, which is easy
// to miss. Also append to userData/sign-in.log so a failed field sign-in
// can be traced after the fact (same pattern as integrations/oauth.ts).
function authLog(msg: string, extra?: unknown): void {
  const line = `[${new Date().toISOString()}] ${msg}${extra !== undefined ? ' ' + JSON.stringify(extra) : ''}`
  console.log('[sign-in]', line)
  try {
    appendFileSync(join(app.getPath('userData'), 'sign-in.log'), line + '\n')
  } catch {
    /* best-effort logging only */
  }
}

function apiBase(): string {
  return resolveWindowsDeployment().apiBase
}

const BETTER_AUTH_STORE_KEY = 'better-auth:session:v1'
let authTokenStore: AuthTokenStore | null = null

function secureStore(): AuthTokenStore {
  if (!authTokenStore) authTokenStore = new AuthTokenStore()
  return authTokenStore
}

function betterAuthClient(): BetterAuthClient {
  return new BetterAuthClient(resolveWindowsDeployment().authBase)
}

function loadBetterAuthSession(): BetterAuthStoredSession | null {
  const raw = secureStore().get(BETTER_AUTH_STORE_KEY)
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as BetterAuthStoredSession
    if (!value.sessionToken || !value.userId) throw new Error('invalid session')
    return value
  } catch {
    secureStore().remove(BETTER_AUTH_STORE_KEY)
    return null
  }
}

function saveBetterAuthSession(session: BetterAuthStoredSession): void {
  secureStore().set(BETTER_AUTH_STORE_KEY, JSON.stringify(session))
}

function betterAuthFailure(error: unknown, stored?: BetterAuthStoredSession): BetterAuthResult {
  const known = error instanceof BetterAuthError ? error : null
  authLog('Better Auth request failed', {
    definitive: known?.definitive ?? false,
    error: known?.message ?? 'unexpected error'
  })
  return {
    ok: false,
    error: known?.message ?? 'Authentication failed unexpectedly.',
    definitive: known?.definitive ?? false,
    user: stored
      ? { uid: stored.userId, email: stored.email, displayName: stored.displayName }
      : undefined
  }
}

/**
 * Register the auth IPC. `onSignedIn` surfaces the main window after the
 * loopback callback lands (the browser holds foreground focus at that point —
 * see index.ts for the focus-steal implementation).
 */
export function registerAuthHandlers(onSignedIn: () => void): void {
  ipcMain.handle('auth:signIn', async (_event, provider: unknown): Promise<SignInResult> => {
    if (resolveWindowsDeployment().identityProvider !== 'firebase') {
      return { ok: false, error: 'Provider OAuth is disabled by this deployment profile.' }
    }
    if (provider !== 'google' && provider !== 'apple') {
      return { ok: false, error: 'Unsupported sign-in provider' }
    }
    authLog(`${provider} sign-in requested`)
    const result = await startSignIn(provider as SignInProvider, {
      apiBase: apiBase(),
      openExternal: (url) => shell.openExternal(url),
      log: authLog
    })
    if (result.ok) {
      authLog(`${provider} sign-in complete — surfacing window`)
      onSignedIn()
    }
    return result
  })

  ipcMain.handle(
    'auth:betterAuthSignIn',
    async (_event, request: BetterAuthSignInRequest): Promise<BetterAuthResult> => {
      if (resolveWindowsDeployment().identityProvider !== 'better_auth') {
        return { ok: false, error: 'Better Auth is disabled by this deployment profile.', definitive: true }
      }
      const email = typeof request?.email === 'string' ? request.email.trim() : ''
      const password = typeof request?.password === 'string' ? request.password : ''
      const name = typeof request?.name === 'string' ? request.name.trim() : undefined
      if (!email || password.length < 8 || (request.createAccount && !name)) {
        return { ok: false, error: 'Enter a valid email, name, and password (8+ characters).', definitive: true }
      }
      try {
        const result = await betterAuthClient().signIn({
          email,
          password,
          createAccount: request.createAccount === true,
          name
        })
        saveBetterAuthSession(result.stored)
        onSignedIn()
        return { ok: true, session: result.session }
      } catch (error) {
        return betterAuthFailure(error)
      }
    }
  )

  ipcMain.handle('auth:betterAuthRestore', async (): Promise<BetterAuthResult> => {
    if (resolveWindowsDeployment().identityProvider !== 'better_auth') {
      return { ok: true, session: null }
    }
    const stored = loadBetterAuthSession()
    if (!stored) return { ok: true, session: null }
    try {
      const result = await betterAuthClient().refresh(stored)
      saveBetterAuthSession(result.stored)
      return { ok: true, session: result.session }
    } catch (error) {
      if (error instanceof BetterAuthError && error.definitive) {
        secureStore().remove(BETTER_AUTH_STORE_KEY)
      }
      return betterAuthFailure(error, stored)
    }
  })

  ipcMain.handle(
    'auth:betterAuthRefresh',
    async (_event, expectedUserId: string): Promise<BetterAuthResult> => {
      if (resolveWindowsDeployment().identityProvider !== 'better_auth') {
        return { ok: false, error: 'Better Auth is disabled by this deployment profile.', definitive: true }
      }
      const stored = loadBetterAuthSession()
      if (!stored || stored.userId !== expectedUserId) {
        secureStore().remove(BETTER_AUTH_STORE_KEY)
        return { ok: false, error: 'Your self-hosted session expired. Sign in again.', definitive: true }
      }
      try {
        const result = await betterAuthClient().refresh(stored)
        saveBetterAuthSession(result.stored)
        return { ok: true, session: result.session }
      } catch (error) {
        if (error instanceof BetterAuthError && error.definitive) {
          secureStore().remove(BETTER_AUTH_STORE_KEY)
        }
        return betterAuthFailure(error, stored)
      }
    }
  )

  ipcMain.handle('auth:betterAuthSignOut', async (): Promise<void> => {
    const stored = loadBetterAuthSession()
    secureStore().remove(BETTER_AUTH_STORE_KEY)
    if (!stored) return
    try {
      await betterAuthClient().signOut(stored.sessionToken)
    } catch (error) {
      authLog('Better Auth sign-out revocation failed', {
        error: error instanceof Error ? error.message : 'unexpected error'
      })
    }
  })
}
