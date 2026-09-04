import { randomUUID } from 'node:crypto'
import { IdentityError, type IdentitySnapshot } from './contract'
import { IdentityClient, type AccessToken, type Credential } from './client'
import type { CredentialStore, StoredIdentity } from './store'

// Main is the sole mutation authority. Network work can overlap; durable
// commits and the complete asynchronous account cleanup share one queue.
export class IdentityOwner {
  private intent = 0
  private intentAbort = new AbortController()
  private leaseAbort = new AbortController()
  private revision = 0
  private clearEpoch = 0
  private cleanupPending = false
  private credential: Credential | null = null
  private lastUserId: string | null = null
  private lease: string | null = null
  private access: AccessToken | null = null
  private issued = new Map<string, number>()
  private mutation = Promise.resolve()
  private problem: IdentitySnapshot['problem'] = null
  private ready: Promise<void> | null = null
  private refresh: { lease: string; promise: Promise<string> } | null = null
  private listeners = new Set<(snapshot: IdentitySnapshot, token: string | null) => void>()

  constructor(
    private client: IdentityClient,
    private store: CredentialStore,
    private cleanup: () => Promise<void>,
    private now = () => Date.now() / 1000
  ) {}

  snapshot(): IdentitySnapshot {
    return {
      revision: this.revision,
      clearEpoch: this.clearEpoch,
      owner: this.lease,
      user: this.credential ? { ...this.credential.user } : null,
      problem: this.problem
    }
  }
  subscribe(fn: (snapshot: IdentitySnapshot, token: string | null) => void): () => void {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }
  private publish(): void {
    this.revision++
    const snapshot = this.snapshot()
    for (const listener of this.listeners) listener(snapshot, this.access?.token ?? null)
  }
  private beginIntent(): number {
    this.intentAbort.abort()
    this.intentAbort = new AbortController()
    return ++this.intent
  }
  private current(attempt: number): void {
    if (this.intent !== attempt) throw new IdentityError('superseded')
  }
  private queued<T>(fn: () => Promise<T> | T): Promise<T> {
    const result = this.mutation.then(fn)
    this.mutation = result.then(
      () => undefined,
      () => undefined
    )
    return result
  }
  private persist(credential: Credential | null, lastUserId: string | null): void {
    this.store.write({
      credential,
      lastUserId,
      clearEpoch: this.clearEpoch,
      cleanupPending: this.cleanupPending
    })
    this.lastUserId = lastUserId
  }
  private async clearAccount(): Promise<void> {
    this.cleanupPending = true
    this.persist(null, this.lastUserId)
    this.adopt(null, null)
    try {
      await this.cleanup()
    } catch {
      this.problem = 'cleanup_required'
      this.publish()
      throw new IdentityError('cleanup_required')
    }
    this.clearEpoch++
    this.cleanupPending = false
    this.persist(null, this.lastUserId)
    this.publish()
  }
  private adopt(credential: Credential | null, access: AccessToken | null): void {
    this.leaseAbort.abort()
    this.leaseAbort = new AbortController()
    this.credential = credential
    this.access = access
    this.issued.clear()
    if (access) this.issued.set(access.token, access.exp)
    this.lease = credential ? randomUUID() : null
    this.problem = null
    this.publish()
  }
  initialize(): Promise<void> {
    if (!this.ready) this.ready = this.restore()
    return this.ready
  }
  private async restore(): Promise<void> {
    const attempt = this.beginIntent()
    let saved: StoredIdentity
    try {
      saved = this.store.read()
      this.lastUserId = saved.lastUserId
      this.clearEpoch = saved.clearEpoch
      this.cleanupPending = saved.cleanupPending
      if (this.cleanupPending) await this.queued(() => this.clearAccount())
      if (!saved.credential) return
      const user = await this.client.restore(saved.credential.session, this.intentAbort.signal)
      this.current(attempt)
      if (!user) {
        this.persist(null, saved.lastUserId)
        return
      }
      if (user.id !== saved.credential.user.id) throw new IdentityError('invalid_response')
      const credential = { session: saved.credential.session, user }
      const access = await this.client.token(credential, this.intentAbort.signal)
      await this.queued(() => {
        this.current(attempt)
        this.persist(credential, user.id)
        this.adopt(credential, access)
      })
    } catch (error) {
      if (attempt !== this.intent) return
      if (error instanceof IdentityError && error.code === 'session_expired')
        this.persist(null, this.lastUserId)
      this.problem = error instanceof IdentityError ? error.code : 'unavailable'
      this.publish()
    }
  }
  async authenticate(input: {
    email: string
    password: string
    name?: string
  }): Promise<IdentitySnapshot> {
    await this.initialize()
    if (this.problem === 'storage_unavailable') await this.restore()
    if (this.problem === 'storage_unavailable' || this.problem === 'storage_corrupt')
      throw new IdentityError(this.problem)
    const attempt = this.beginIntent()
    const credential = await this.client.authenticate(input, this.intentAbort.signal)
    this.current(attempt)
    const access = await this.client.token(credential, this.intentAbort.signal)
    await this.queued(async () => {
      this.current(attempt)
      if (this.cleanupPending || (this.lastUserId && this.lastUserId !== credential.user.id)) {
        // Abort old workers before clearing stores. No new owner can publish
        // until the entire cleanup settles, including late async DB/file work.
        await this.clearAccount()
        this.current(attempt)
      }
      this.persist(credential, credential.user.id)
      this.adopt(credential, access)
    })
    return this.snapshot()
  }
  ownsAccessToken(token: string): boolean {
    return !!this.lease && (this.issued.get(token) ?? 0) > this.now()
  }
  async token(lease: string, force = false): Promise<string> {
    const credential = this.credential
    if (!credential || lease !== this.lease) throw new IdentityError('session_expired')
    if (!force && this.access && this.access.exp > this.now() + 30) return this.access.token
    if (this.refresh?.lease === lease) return this.refresh.promise
    const promise = (async () => {
      try {
        const access = await this.client.token(credential, this.leaseAbort.signal)
        if (lease !== this.lease) throw new IdentityError('superseded')
        if (this.access && this.access.sid !== access.sid)
          throw new IdentityError('invalid_response')
        this.access = access
        for (const [token, exp] of this.issued) if (exp <= this.now()) this.issued.delete(token)
        this.issued.set(access.token, access.exp)
        this.publish()
        return access.token
      } catch (error) {
        if (
          error instanceof IdentityError &&
          error.code === 'session_expired' &&
          lease === this.lease
        ) {
          await this.invalidate(lease)
        }
        throw error
      } finally {
        if (this.refresh?.lease === lease) this.refresh = null
      }
    })()
    this.refresh = { lease, promise }
    return promise
  }
  async updateName(lease: string, name: string): Promise<IdentitySnapshot> {
    const credential = this.credential
    const attempt = this.intent
    if (!credential || this.lease !== lease) throw new IdentityError('session_expired')
    const user = await this.client.updateName(credential, name, this.intentAbort.signal)
    await this.queued(() => {
      this.current(attempt)
      if (this.lease !== lease) throw new IdentityError('superseded')
      this.persist({ ...credential, user }, user.id)
      this.credential = { ...credential, user }
      this.publish()
    })
    return this.snapshot()
  }
  async invalidate(lease: string): Promise<void> {
    if (lease !== this.lease) return
    const attempt = this.beginIntent()
    await this.queued(() => {
      this.current(attempt)
      this.persist(null, this.lastUserId)
      this.adopt(null, null)
    })
  }
  async signOut(lease: string): Promise<IdentitySnapshot> {
    const credential = this.credential
    if (!credential || lease !== this.lease) throw new IdentityError('session_expired')
    const attempt = this.beginIntent()
    // A deliberate logout must revoke the server session before discarding it.
    // Transport failure preserves it so the user can retry the same action.
    await this.client.signOut(credential.session, this.intentAbort.signal)
    await this.queued(async () => {
      this.current(attempt)
      this.persist(null, this.lastUserId)
      this.adopt(null, null)
      await this.clearAccount()
      this.current(attempt)
      this.persist(null, null)
    })
    return this.snapshot()
  }
}
