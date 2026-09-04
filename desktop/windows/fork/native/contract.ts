// LIFECYCLE: permanent
// Only these public values may cross the main/renderer boundary. Opaque sessions
// have a separate main-only type in client.ts and never appear in this contract.
export interface IdentityUser {
  id: string
  email: string
  name: string
}
export interface IdentitySnapshot {
  revision: number
  clearEpoch: number
  owner: string | null
  user: IdentityUser | null
  problem: IdentityErrorCode | null
}
export type IdentityErrorCode =
  | 'invalid_credentials'
  | 'session_expired'
  | 'unavailable'
  | 'invalid_response'
  | 'storage_unavailable'
  | 'storage_corrupt'
  | 'superseded'
  | 'cleanup_required'
  | 'invalid_input'

const messages: Record<IdentityErrorCode, string> = {
  invalid_credentials: 'Email or password was not accepted.',
  session_expired: 'Your session expired. Please sign in again.',
  unavailable: 'The identity service is unavailable. Please try again.',
  invalid_response: 'The identity service returned an invalid response.',
  storage_unavailable: 'Secure storage is unavailable. Unlock your system keyring and try again.',
  storage_corrupt: 'The saved credential could not be read. Your existing data was preserved.',
  superseded: 'A newer account action replaced this request.',
  cleanup_required: 'Finish clearing the previous account before signing in.',
  invalid_input: 'Check the account details and try again.'
}
export class IdentityError extends Error {
  constructor(readonly code: IdentityErrorCode) {
    super(messages[code])
  }
}
export type IdentityResult<T> = { ok: true; value: T } | { ok: false; code: IdentityErrorCode }
export interface IdentityBridge {
  snapshot(): Promise<IdentityResult<IdentitySnapshot>>
  authenticate(input: {
    email: string
    password: string
    name?: string
  }): Promise<IdentityResult<IdentitySnapshot>>
  token(owner: string, force: boolean): Promise<IdentityResult<string>>
  updateName(owner: string, name: string): Promise<IdentityResult<IdentitySnapshot>>
  signOut(owner: string): Promise<IdentityResult<IdentitySnapshot>>
  invalidate(owner: string): Promise<IdentityResult<void>>
  onChanged(callback: (snapshot: IdentitySnapshot) => void): () => void
}

export function unwrap<T>(result: IdentityResult<T>): T {
  if (!result.ok) throw new IdentityError(result.code)
  return result.value
}

declare global {
  interface Window {
    forkIdentity: IdentityBridge
  }
}
