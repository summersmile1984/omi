import { IdentityError, unwrap, type IdentitySnapshot } from '../native/contract'
import { setPreferences, resetOnboarding } from '../../src/renderer/src/lib/preferences'
import { clearRendererUserData } from '../../src/renderer/src/lib/authTeardown'

export interface User {
  uid: string
  email: string
  displayName: string
  photoURL: null
  readonly owner: string
  getIdToken(force?: boolean): Promise<string>
}
type Listener = (user: User | null) => void
const listeners = new Set<Listener>()
let revision = -1
let settled = false
let problem: IdentitySnapshot['problem'] = null
let clearEpoch = Number(localStorage.getItem('fork.identity.clearEpoch') ?? '0')

export const auth: { currentUser: User | null; authStateReady(): Promise<void> } = {
  currentUser: null,
  authStateReady: () => ready
}
function adopt(snapshot: IdentitySnapshot): void {
  if (snapshot.revision <= revision) return
  revision = snapshot.revision
  problem = snapshot.problem
  const previous = auth.currentUser
  const previousName = previous?.displayName
  const previousEmail = previous?.email
  // Main has already finished its asynchronous account teardown. Renderer
  // projections clear synchronously, before any listener sees the new owner.
  if (clearEpoch !== snapshot.clearEpoch) {
    clearRendererUserData()
    clearEpoch = snapshot.clearEpoch
    localStorage.setItem('fork.identity.clearEpoch', String(clearEpoch))
  }
  if (snapshot.owner && snapshot.user) {
    const owner = snapshot.owner
    // Preserve User object identity on same-owner refresh; existing capture and
    // request hosts pin the object across awaits to reject departed accounts.
    const user =
      previous?.owner === owner
        ? previous
        : {
            owner,
            uid: snapshot.user.id,
            email: snapshot.user.email,
            displayName: snapshot.user.name,
            photoURL: null,
            async getIdToken(force = false): Promise<string> {
              if (auth.currentUser?.owner !== owner) throw new IdentityError('session_expired')
              const token = unwrap(await window.forkIdentity.token(owner, force))
              if (auth.currentUser?.owner !== owner) throw new IdentityError('superseded')
              return token
            }
          }
    user.displayName = snapshot.user.name
    user.email = snapshot.user.email
    const previousUid = localStorage.getItem('omi.lastSignedInUid')
    if (previousUid && previousUid !== user.uid) resetOnboarding()
    localStorage.setItem('omi.lastSignedInUid', user.uid)
    setPreferences({ displayName: user.displayName })
    auth.currentUser = user
  } else auth.currentUser = null
  const changed =
    !settled ||
    previous !== auth.currentUser ||
    previousName !== auth.currentUser?.displayName ||
    previousEmail !== auth.currentUser?.email
  settled = true
  if (changed) for (const listener of listeners) listener(auth.currentUser)
}
window.forkIdentity.onChanged(adopt)
const ready = window.forkIdentity
  .snapshot()
  .then((result) => adopt(unwrap(result)))
  .catch((error) => {
    problem = error instanceof IdentityError ? error.code : 'unavailable'
    settled = true
    for (const listener of listeners) listener(null)
  })

export function identityProblem(): string | null {
  return problem ? new IdentityError(problem).message : null
}
export function onAuthStateChanged(_auth: typeof auth, callback: Listener): () => void {
  listeners.add(callback)
  if (settled)
    queueMicrotask(() => {
      if (listeners.has(callback)) callback(auth.currentUser)
    })
  return () => listeners.delete(callback)
}
export async function authenticate(input: {
  email: string
  password: string
  name?: string
}): Promise<void> {
  adopt(unwrap(await window.forkIdentity.authenticate(input)))
}
export async function updateProfile(user: User, patch: { displayName?: string }): Promise<void> {
  if (user !== auth.currentUser || !patch.displayName) throw new IdentityError('invalid_input')
  adopt(unwrap(await window.forkIdentity.updateName(user.owner, patch.displayName)))
}
export async function signOutUser(): Promise<void> {
  const user = auth.currentUser
  if (!user) return
  adopt(unwrap(await window.forkIdentity.signOut(user.owner)))
}
