// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'

const firebase = vi.hoisted(() => ({ initializeApp: vi.fn(), initializeAuth: vi.fn() }))

vi.mock('firebase/app', () => ({ initializeApp: firebase.initializeApp }))
vi.mock('firebase/auth', () => ({
  browserLocalPersistence: {},
  getAuth: vi.fn(),
  initializeAuth: firebase.initializeAuth,
  onIdTokenChanged: vi.fn(),
  signInWithCustomToken: vi.fn(),
  signOut: vi.fn(),
  updateProfile: vi.fn()
}))
vi.mock('./encryptedAuthPersistence', () => ({
  encryptedAuthPersistence: {},
  scrubLegacyPlaintextAuth: vi.fn()
}))
vi.mock('./authTeardown', () => ({ teardownUserData: vi.fn() }))

function selfHostedEnv(): void {
  vi.stubEnv('VITE_OMI_DEPLOYMENT_PROFILE', 'self_hosted')
  vi.stubEnv('VITE_OMI_IDENTITY_PROVIDER', 'better_auth')
  vi.stubEnv('VITE_OMI_API_BASE', 'https://api.example.test')
  vi.stubEnv('VITE_OMI_DESKTOP_API_BASE', 'https://desktop.example.test')
  vi.stubEnv('VITE_OMI_AUTH_BASE', 'https://auth.example.test')
  vi.stubEnv('VITE_OMI_MCP_BASE', 'https://mcp.example.test')
  vi.stubEnv('VITE_POSTHOG_KEY', '')
  vi.stubEnv('VITE_OMI_ANALYTICS_BASE', '')
}

beforeEach(() => {
  vi.resetModules()
  vi.unstubAllEnvs()
  selfHostedEnv()
  firebase.initializeApp.mockReset()
  firebase.initializeAuth.mockReset()
})

describe('self-hosted identity lifecycle', () => {
  it('preserves the encrypted session identity across a transient restore outage and refreshes later', async () => {
    const restore = vi.fn().mockResolvedValue({
      ok: false,
      definitive: false,
      error: 'offline',
      user: { uid: 'u1', email: 'owner@example.test' }
    })
    const refresh = vi.fn().mockResolvedValue({
      ok: true,
      session: {
        user: { uid: 'u1', email: 'owner@example.test' },
        token: 'fresh-jwt',
        expiresAt: Date.now() + 900_000
      }
    })
    Object.defineProperty(window, 'omi', {
      configurable: true,
      value: { betterAuthRestore: restore, betterAuthRefresh: refresh }
    })

    const identity = await import('./identity')
    await vi.waitFor(() => expect(identity.auth.currentUser?.uid).toBe('u1'))
    expect(firebase.initializeApp).not.toHaveBeenCalled()
    await expect(identity.auth.currentUser?.getIdToken()).resolves.toBe('fresh-jwt')
    expect(refresh).toHaveBeenCalledWith('u1')
  })

  it('emits signed-out only after a definitive restored-session rejection', async () => {
    Object.defineProperty(window, 'omi', {
      configurable: true,
      value: {
        betterAuthRestore: vi.fn().mockResolvedValue({
          ok: false,
          definitive: true,
          error: 'expired',
          user: { uid: 'u1' }
        })
      }
    })

    const identity = await import('./identity')
    const states: Array<string | null> = []
    identity.onAuthStateChanged(identity.auth, (user) => states.push(user?.uid ?? null))
    await vi.waitFor(() => expect(states).toEqual([null]))
    expect(firebase.initializeApp).not.toHaveBeenCalled()
  })
})
