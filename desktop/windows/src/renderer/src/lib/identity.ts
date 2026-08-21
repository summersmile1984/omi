// Provider-neutral renderer identity authority. Self-hosted releases never
// initialize Firebase: main owns an opaque Better Auth database session and this
// facade exposes only its short-lived JWT through the same getIdToken contract.
import { initializeApp } from 'firebase/app'
import {
  browserLocalPersistence,
  getAuth,
  initializeAuth,
  onIdTokenChanged as onFirebaseIdTokenChanged,
  signInWithCustomToken,
  signOut as firebaseSignOut,
  updateProfile,
  type Auth as FirebaseAuth,
  type User as FirebaseUser
} from 'firebase/auth'
import { teardownUserData } from './authTeardown'
import { encryptedAuthPersistence, scrubLegacyPlaintextAuth } from './encryptedAuthPersistence'
import { resolveWindowsDeployment } from '../../../shared/deploymentProfile'
import type { BetterAuthSession, BetterAuthUser, SignInProvider } from '../../../shared/types'

export type IdentityUser = {
  uid: string
  email: string | null
  displayName: string | null
  photoURL?: string | null
  getIdToken: (forceRefresh?: boolean) => Promise<string>
}

type AuthListener = (user: IdentityUser | null) => void

const deployment = resolveWindowsDeployment()
const authListeners = new Set<AuthListener>()
const tokenListeners = new Set<AuthListener>()
let initialAuthResolved = false
let resolveInitialAuth!: () => void
const initialAuthReady = new Promise<void>((resolve) => {
  resolveInitialAuth = resolve
})
let betterAuthToken: { token: string; expiresAt: number } | null = null
let betterAuthRefresh: Promise<string> | null = null
let betterAuthRefreshTimer: ReturnType<typeof setTimeout> | null = null

/** Stable provider-neutral object consumed by every renderer surface. */
export const auth: {
  currentUser: IdentityUser | null
  authStateReady: () => Promise<void>
} = { currentUser: null, authStateReady: () => initialAuthReady }

function emit(user: IdentityUser | null, tokenChanged = true): void {
  auth.currentUser = user
  if (!initialAuthResolved) {
    initialAuthResolved = true
    resolveInitialAuth()
  }
  for (const listener of authListeners) listener(user)
  if (tokenChanged) for (const listener of tokenListeners) listener(user)
}

function subscribe(set: Set<AuthListener>, listener: AuthListener): () => void {
  set.add(listener)
  if (initialAuthResolved) queueMicrotask(() => set.has(listener) && listener(auth.currentUser))
  return () => set.delete(listener)
}

export function onAuthStateChanged(_auth: unknown, listener: AuthListener): () => void {
  return subscribe(authListeners, listener)
}

export function onIdTokenChanged(_auth: unknown, listener: AuthListener): () => void {
  return subscribe(tokenListeners, listener)
}

let firebaseAuth: FirebaseAuth | null = null
if (deployment.identityProvider === 'firebase') {
  const app = initializeApp({
    apiKey: import.meta.env.VITE_FIREBASE_API_KEY as string,
    authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN as string,
    projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID as string
  })
  try {
    firebaseAuth = initializeAuth(app, {
      persistence: [encryptedAuthPersistence, browserLocalPersistence]
    })
  } catch {
    firebaseAuth = getAuth(app)
  }
  onFirebaseIdTokenChanged(firebaseAuth, (user) => {
    emit(user)
    if (user) void scrubLegacyPlaintextAuth()
  })
} else {
  void restoreBetterAuth()
}

function clearBetterAuthTimer(): void {
  if (betterAuthRefreshTimer) clearTimeout(betterAuthRefreshTimer)
  betterAuthRefreshTimer = null
}

function scheduleBetterAuthRefresh(session: BetterAuthSession): void {
  clearBetterAuthTimer()
  const delay = Math.max(30_000, session.expiresAt - Date.now() - 5 * 60_000)
  betterAuthRefreshTimer = setTimeout(() => {
    void refreshBetterAuthToken(true).catch(() => {
      // A transient refresh failure retains the main-process database session;
      // retry in one minute. Definitive failure already emitted signed-out.
      if (auth.currentUser) {
        betterAuthRefreshTimer = setTimeout(
          () => void refreshBetterAuthToken(true).catch(() => {}),
          60_000
        )
      }
    })
  }, delay)
}

function scheduleTransientIdentityRefresh(attempt = 0): void {
  clearBetterAuthTimer()
  const delay = Math.min(60_000, 5_000 * 2 ** Math.min(attempt, 4))
  betterAuthRefreshTimer = setTimeout(() => {
    if (!auth.currentUser) return
    void refreshBetterAuthToken(true).catch((error: { code?: string }) => {
      if (error?.code === 'auth/network-request-failed' && auth.currentUser) {
        scheduleTransientIdentityRefresh(attempt + 1)
      }
    })
  }, delay)
}

function makeBetterAuthUser(user: BetterAuthUser): IdentityUser {
  return {
    uid: user.uid,
    email: user.email ?? null,
    displayName: user.displayName ?? null,
    photoURL: null,
    getIdToken: (forceRefresh = false) => refreshBetterAuthToken(forceRefresh)
  }
}

function applyBetterAuthSession(session: BetterAuthSession): IdentityUser {
  betterAuthToken = { token: session.token, expiresAt: session.expiresAt }
  const user = makeBetterAuthUser(session.user)
  auth.currentUser = user
  scheduleBetterAuthRefresh(session)
  return user
}

async function restoreBetterAuth(attempt = 0): Promise<void> {
  try {
    const result = await window.omi.betterAuthRestore()
    if (!result.ok) {
      if (result.definitive) {
        emit(null)
      } else if (result.user) {
        // Preserve the encrypted database session's known identity through an
        // auth-service outage. Backend calls will request a fresh JWT and retry
        // with a capped backoff; only a definitive rejection emits signed-out.
        betterAuthToken = null
        emit(makeBetterAuthUser(result.user))
        scheduleTransientIdentityRefresh(attempt)
      } else {
        betterAuthRefreshTimer = setTimeout(
          () => void restoreBetterAuth(attempt + 1),
          Math.min(60_000, 5_000 * 2 ** Math.min(attempt, 4))
        )
      }
      return
    }
    if (!result.session) {
      emit(null)
      return
    }
    emit(applyBetterAuthSession(result.session))
  } catch {
    // The encrypted session remains main-side on transient startup failure.
    betterAuthRefreshTimer = setTimeout(
      () => void restoreBetterAuth(attempt + 1),
      Math.min(60_000, 5_000 * 2 ** Math.min(attempt, 4))
    )
  }
}

async function refreshBetterAuthToken(forceRefresh: boolean): Promise<string> {
  const user = auth.currentUser
  const cached = betterAuthToken
  if (!user) {
    throw Object.assign(new Error('No active session'), { code: 'auth/invalid-user-token' })
  }
  if (!forceRefresh && cached && cached.expiresAt - Date.now() > 5 * 60_000) return cached.token
  if (betterAuthRefresh) return betterAuthRefresh

  const expectedUser = user
  betterAuthRefresh = (async () => {
    try {
      const result = await window.omi.betterAuthRefresh(expectedUser.uid)
      if (!result.ok || !result.session) {
        if (!result.ok && result.definitive && auth.currentUser === expectedUser) {
          clearBetterAuthTimer()
          betterAuthToken = null
          emit(null)
        }
        throw Object.assign(new Error(result.ok ? 'Session unavailable' : result.error), {
          code:
            !result.ok && result.definitive
              ? 'auth/invalid-user-token'
              : 'auth/network-request-failed'
        })
      }
      if (auth.currentUser !== expectedUser) throw new Error('Auth state changed during refresh')
      const refreshed = applyBetterAuthSession(result.session)
      // A token refresh does not change account identity, but token consumers
      // must receive the new JWT (pi-mono, embeddings, AI profile).
      for (const listener of tokenListeners) listener(refreshed)
      return result.session.token
    } finally {
      betterAuthRefresh = null
    }
  })()
  return betterAuthRefresh
}

export async function signInWithBetterAuth(request: {
  email: string
  password: string
  createAccount: boolean
  name?: string
}): Promise<IdentityUser> {
  if (deployment.identityProvider !== 'better_auth') throw new Error('Better Auth is not enabled')
  const result = await window.omi.betterAuthSignIn(request)
  if (!result.ok || !result.session) throw new Error(result.ok ? 'No session returned' : result.error)
  const user = applyBetterAuthSession(result.session)
  emit(user)
  return user
}

/** Official cloud OAuth path; unreachable in self_hosted builds. */
export async function signInWithProvider(provider: SignInProvider): Promise<IdentityUser> {
  if (!firebaseAuth) throw new Error('Provider OAuth is disabled by this deployment profile')
  const result = await window.omi.signInWithProvider(provider)
  if (!result.ok) throw new Error(result.error)
  const cred = await signInWithCustomToken(firebaseAuth, result.customToken)
  const name = [result.givenName, result.familyName].filter(Boolean).join(' ')
  if (name && !cred.user.displayName) {
    try {
      await updateProfile(cred.user, { displayName: name })
    } catch {
      /* cosmetic only */
    }
  }
  return cred.user
}

/** Light session invalidation used by the refresh coordinator. */
export async function signOutIdentitySession(): Promise<void> {
  clearBetterAuthTimer()
  betterAuthToken = null
  if (firebaseAuth) {
    await firebaseSignOut(firebaseAuth)
  } else {
    await window.omi.betterAuthSignOut()
    emit(null)
  }
}

export async function signOutUser(): Promise<void> {
  const token = await auth.currentUser?.getIdToken().catch(() => undefined)
  await teardownUserData()
  if (token && deployment.allowByok) {
    try {
      await window.omi.byokDeactivate(token)
    } catch {
      /* best-effort */
    }
  }
  await signOutIdentitySession()
}

/** Cloud-only escape hatch for the one profile-editing call site. */
export function firebaseUser(user: IdentityUser): FirebaseUser | null {
  return firebaseAuth?.currentUser?.uid === user.uid ? firebaseAuth.currentUser : null
}
