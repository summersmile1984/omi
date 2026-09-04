// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import type { IdentitySnapshot } from '../../native/contract'
const ports = vi.hoisted(() => ({ preferences: vi.fn(), clear: vi.fn(), reset: vi.fn() }))
vi.mock('../../../src/renderer/src/lib/preferences', () => ({
  setPreferences: ports.preferences,
  resetOnboarding: ports.reset
}))
vi.mock('../../../src/renderer/src/lib/authTeardown', () => ({
  clearRendererUserData: ports.clear
}))

function state(revision: number, uid: string | null, clearEpoch = 0): IdentitySnapshot {
  return {
    revision,
    clearEpoch,
    owner: uid ? 'lease-' + uid : null,
    user: uid ? { id: uid, email: uid + '@example.invalid', name: uid } : null,
    problem: null
  }
}
let changed: (snapshot: IdentitySnapshot) => void
let current = state(0, null)
let finishToken: (value: unknown) => void
beforeEach(() => {
  vi.resetModules()
  vi.clearAllMocks()
  localStorage.clear()
  current = state(0, null)
  window.forkIdentity = {
    onChanged: vi.fn((callback) => {
      changed = callback
      return () => {}
    }),
    snapshot: vi.fn(async () => ({ ok: true, value: current })),
    authenticate: vi.fn(async () => ({ ok: true, value: current })),
    updateName: vi.fn(async () => ({ ok: false, code: 'unavailable' })),
    signOut: vi.fn(async () => ({ ok: false, code: 'unavailable' })),
    invalidate: vi.fn(async () => ({ ok: true, value: undefined })),
    token: vi.fn(
      () =>
        new Promise((resolve) => {
          finishToken = resolve
        })
    )
  }
})
it('stable same-owner token refresh does not repeat auth transitions, and stale User cannot obtain B credentials', async () => {
  const identity = await import('../../renderer/identity')
  await identity.auth.authStateReady()
  const listener = vi.fn()
  identity.onAuthStateChanged(identity.auth, listener)
  await Promise.resolve()
  listener.mockClear()
  changed(state(1, 'a'))
  const a = identity.auth.currentUser!
  expect(listener).toHaveBeenCalledTimes(1)
  changed(state(2, 'a'))
  expect(identity.auth.currentUser).toBe(a)
  expect(listener).toHaveBeenCalledTimes(1)
  const pending = a.getIdToken(true)
  changed(state(3, 'b', 1))
  finishToken({ ok: true, value: 'access-a' })
  await expect(pending).rejects.toMatchObject({ code: 'superseded' })
  await expect(a.getIdToken()).rejects.toMatchObject({ code: 'session_expired' })
  expect(ports.clear).toHaveBeenCalledTimes(1)
  expect(ports.clear.mock.invocationCallOrder[0]).toBeLessThan(
    ports.preferences.mock.invocationCallOrder.at(-1)!
  )
  expect(localStorage.getItem('omi.lastSignedInUid')).toBe('b')
  changed(state(2, 'a'))
  expect(identity.auth.currentUser?.uid).toBe('b')
})
it('failed canonical name / remote logout preserves identity and displayed preferences', async () => {
  current = state(1, 'a')
  const identity = await import('../../renderer/identity')
  await identity.auth.authStateReady()
  ports.preferences.mockClear()
  await expect(
    identity.updateProfile(identity.auth.currentUser!, { displayName: 'Unconfirmed' })
  ).rejects.toMatchObject({ code: 'unavailable' })
  await expect(identity.signOutUser()).rejects.toMatchObject({ code: 'unavailable' })
  expect(identity.auth.currentUser?.displayName).toBe('a')
  expect(ports.preferences).not.toHaveBeenCalled()
  expect(ports.clear).not.toHaveBeenCalled()
})
