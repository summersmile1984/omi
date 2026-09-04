import { IdentityError, unwrap } from '../native/contract'
import { auth } from './identity'
import { toast } from '../../src/renderer/src/lib/toast'

export type RefreshOutcome =
  | { status: 'ok'; token: string }
  | { status: 'dead' }
  | { status: 'transient' }
export async function refreshIdToken(): Promise<RefreshOutcome> {
  const user = auth.currentUser
  if (!user) return { status: 'dead' }
  try {
    return { status: 'ok', token: await user.getIdToken(true) }
  } catch (error) {
    return {
      status:
        error instanceof IdentityError && error.code === 'session_expired' ? 'dead' : 'transient'
    }
  }
}
export async function forceReauth(): Promise<void> {
  const user = auth.currentUser
  if (!user) return
  unwrap(await window.forkIdentity.invalidate(user.owner))
  toast('Your session expired — please sign in again', { tone: 'warn' })
}
