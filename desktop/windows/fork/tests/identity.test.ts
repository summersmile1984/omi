import { describe, expect, it } from 'vitest'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { IdentityClient, admitToken } from '../native/client'
import { IdentityOwner } from '../native/owner'
import { SecureCredentialStore, type StoredIdentity } from '../native/store'

const now = 1_800_000_000
function jwt(uid: string, fields: Record<string, unknown> = {}): string {
  return [
    'eyJhbGciOiJFUzI1NiJ9',
    Buffer.from(
      JSON.stringify({ uid, sub: uid, sid: 'session-' + uid, iat: now, exp: now + 3600, ...fields })
    ).toString('base64url'),
    'signature-fixture'
  ].join('.')
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}
function fixture(cleanup: () => Promise<void> = async () => {}) {
  let stored: StoredIdentity = {
    credential: null,
    lastUserId: null,
    clearEpoch: 0,
    cleanupPending: false
  }
  let fault: number | null = null
  let nullSession = false
  let tokenFields: Record<string, unknown> = {}
  const requests: { url: string; init: RequestInit }[] = []
  let block: ((url: string, init: RequestInit) => Promise<Response> | null) | null = null
  const names = new Map<string, string>()
  const fetchImpl: typeof fetch = async (url, init = {}) => {
    requests.push({ url: String(url), init })
    const blocked = block?.(String(url), init)
    if (blocked) return blocked
    if (fault) return new Response('{}', { status: fault })
    const body = init.body ? JSON.parse(String(init.body)) : {}
    const session = new Headers(init.headers).get('Authorization')?.replace('Bearer opaque-', '')
    const uid = body.email?.split('@')[0] ?? session
    const user = { id: uid, email: `${uid}@example.invalid`, name: names.get(uid) ?? uid }
    if (String(url).endsWith('/token')) return Response.json({ token: jwt(uid, tokenFields) })
    if (String(url).endsWith('/get-session')) return Response.json(nullSession ? null : { user })
    if (String(url).endsWith('/update-user')) {
      names.set(uid, body.name)
      return Response.json({ status: true })
    }
    if (String(url).endsWith('/sign-out')) return Response.json({ success: true })
    return Response.json({ user }, { headers: { 'set-auth-token': `opaque-${uid}` } })
  }
  const store = {
    read: () => structuredClone(stored),
    write: (value: StoredIdentity) => {
      stored = structuredClone(value)
    }
  }
  const client = new IdentityClient('https://auth.example.invalid', fetchImpl, () => now)
  return {
    owner: new IdentityOwner(client, store, cleanup, () => now),
    client,
    store,
    requests,
    state: () => stored,
    fault: (status: number | null) => {
      fault = status
    },
    nullSession: () => {
      nullSession = true
    },
    tokenFields: (value: Record<string, unknown>) => {
      tokenFields = value
    },
    block: (fn: typeof block) => {
      block = fn
    }
  }
}
const login = (email: string) => ({
  email: `${email}@example.invalid`,
  password: 'fixture-password'
})

describe('production IdentityClient and main owner', () => {
  it('logs in, restores only from opaque secure storage, exchanges JWT and revokes before clearing', async () => {
    const f = fixture()
    await f.owner.authenticate(login('a'))
    const { owner } = f.owner.snapshot()
    expect(f.state().credential?.session).toBe('opaque-a')
    expect(JSON.stringify(f.owner.snapshot())).not.toContain('opaque-')
    expect(await f.owner.token(owner!, true)).toBe(jwt('a'))
    const restored = new IdentityOwner(f.client, f.store, async () => {})
    await restored.initialize()
    expect(restored.snapshot().user?.id).toBe('a')
    await restored.signOut(restored.snapshot().owner!)
    expect(f.state().credential).toBeNull()
    expect(restored.snapshot().user).toBeNull()
    expect(f.state().clearEpoch).toBe(1)
    expect(f.requests.at(-1)?.url).toMatch(/sign-out$/)
    expect(f.requests.every(({ init }) => init.redirect === 'error' && init.signal)).toBe(true)
  })
  it('preserves credentials and published identity on service or malformed-token failure; definitive 401 clears only credential', async () => {
    let cleared = 0
    const f = fixture(async () => {
      cleared++
    })
    await f.owner.authenticate(login('a'))
    const owner = f.owner.snapshot().owner!
    f.fault(503)
    await expect(f.owner.token(owner, true)).rejects.toMatchObject({ code: 'unavailable' })
    await expect(f.owner.signOut(owner)).rejects.toMatchObject({ code: 'unavailable' })
    expect(f.state().credential?.session).toBe('opaque-a')
    f.fault(null)
    f.tokenFields({ uid: 'b' })
    await expect(f.owner.token(owner, true)).rejects.toMatchObject({ code: 'invalid_response' })
    expect(f.owner.snapshot().owner).toBe(owner)
    f.fault(401)
    await expect(f.owner.token(owner, true)).rejects.toMatchObject({ code: 'session_expired' })
    expect(f.owner.snapshot().user).toBeNull()
    expect(f.state().lastUserId).toBe('a')
    expect(cleared).toBe(0)
  })
  it('does not reassign the local principal from an authenticated but mismatched restore', async () => {
    const f = fixture()
    await f.owner.authenticate(login('a'))
    f.block((url) =>
      url.endsWith('/get-session')
        ? Promise.resolve(
            Response.json({ user: { id: 'b', email: 'b@example.invalid', name: 'b' } })
          )
        : null
    )
    const restored = new IdentityOwner(f.client, f.store, async () => {})
    await restored.initialize()
    expect(restored.snapshot()).toMatchObject({ user: null, problem: 'invalid_response' })
    expect(f.state().credential?.user.id).toBe('a')
  })
  it('null and 401 restored sessions are definitive; unavailable restore keeps saved opaque material', async () => {
    const f = fixture()
    await f.owner.authenticate(login('a'))
    f.fault(503)
    const offline = new IdentityOwner(f.client, f.store, async () => {})
    await offline.initialize()
    expect(offline.snapshot()).toMatchObject({ user: null, problem: 'unavailable' })
    expect(f.state().credential).not.toBeNull()
    f.fault(null)
    f.nullSession()
    const expired = new IdentityOwner(f.client, f.store, async () => {})
    await expired.initialize()
    expect(f.state().credential).toBeNull()
    expect(f.state().lastUserId).toBe('a')
  })
  it('newest login intent wins and a late login cannot republish after logout', async () => {
    const f = fixture()
    await f.owner.authenticate(login('initial'))
    const wait = deferred<Response>()
    f.block((url, init) =>
      url.endsWith('/sign-in/email') && String(init.body).includes('"a@') ? wait.promise : null
    )
    const a = f.owner.authenticate(login('a'))
    await Promise.resolve()
    await Promise.resolve()
    await f.owner.authenticate(login('b'))
    expect(
      f.requests.find(({ init }) => String(init.body).includes('\"a@'))?.init.signal?.aborted
    ).toBe(true)
    wait.resolve(
      Response.json(
        { user: { id: 'a', email: 'a@example.invalid', name: 'a' } },
        { headers: { 'set-auth-token': 'opaque-a' } }
      )
    )
    await expect(a).rejects.toMatchObject({ code: 'superseded' })
    expect(f.state().credential?.user.id).toBe('b')
    const late = deferred<Response>()
    f.block((url) => (url.endsWith('/sign-in/email') ? late.promise : null))
    const pending = f.owner.authenticate(login('a'))
    await Promise.resolve()
    await Promise.resolve()
    await f.owner.signOut(f.owner.snapshot().owner!)
    late.resolve(
      Response.json(
        { user: { id: 'a', email: 'a@example.invalid', name: 'a' } },
        { headers: { 'set-auth-token': 'opaque-a' } }
      )
    )
    await expect(pending).rejects.toMatchObject({ code: 'superseded' })
    expect(f.owner.snapshot().user).toBeNull()
  })
  it('serializes the whole async cleanup before a later login publishes or writes its data', async () => {
    const wait = deferred<void>(),
      entered = deferred<void>()
    let userData = 'a'
    const f = fixture(async () => {
      entered.resolve()
      await wait.promise
      userData = ''
    })
    await f.owner.authenticate(login('a'))
    const logout = f.owner.signOut(f.owner.snapshot().owner!)
    await entered.promise
    const b = f.owner.authenticate(login('b'))
    await Promise.resolve()
    await Promise.resolve()
    expect(f.owner.snapshot().user).toBeNull()
    wait.resolve()
    await expect(logout).rejects.toMatchObject({ code: 'superseded' })
    await b
    userData = 'b'
    expect(f.state().credential?.user.id).toBe('b')
    expect(userData).toBe('b')
    expect(f.state().cleanupPending).toBe(false)
  })
  it('persists incomplete cleanup and retries it after process restart before admitting a new owner', async () => {
    const f = fixture(async () => {
      throw new Error('disk busy')
    })
    await f.owner.authenticate(login('a'))
    await expect(f.owner.signOut(f.owner.snapshot().owner!)).rejects.toMatchObject({
      code: 'cleanup_required'
    })
    expect(f.state().cleanupPending).toBe(true)
    let cleared = 0
    const restored = new IdentityOwner(f.client, f.store, async () => {
      cleared++
    })
    await restored.initialize()
    await restored.authenticate(login('b'))
    expect(cleared).toBeGreaterThan(0)
    expect(f.state().cleanupPending).toBe(false)
    expect(restored.snapshot().user?.id).toBe('b')
  })
  it('name completion cannot overwrite a newer identity; confirmed names are canonical', async () => {
    const f = fixture()
    await f.owner.authenticate(login('a'))
    await f.owner.updateName(f.owner.snapshot().owner!, 'New name')
    expect(f.owner.snapshot().user?.name).toBe('New name')
    const wait = deferred<Response>()
    f.block((url) => (url.endsWith('/update-user') ? wait.promise : null))
    const pending = f.owner.updateName(f.owner.snapshot().owner!, 'Late name')
    await f.owner.authenticate(login('b'))
    wait.resolve(Response.json({ status: true }))
    await expect(pending).rejects.toMatchObject({ code: 'superseded' })
    expect(f.owner.snapshot().user?.id).toBe('b')
  })
})

describe('client cache admission (API/WS authority is unchanged)', () => {
  it('allows the agreed 60-second future iat bound, never expiration or an invalid principal/session', () => {
    for (const skew of [0, 30, 60])
      expect(
        admitToken(jwt('a', { iat: now + skew, exp: now + 3600 }), 'a', now).token
      ).toBeTruthy()
    for (const fields of [
      { iat: now + 61 },
      { exp: now },
      { iat: -1 },
      { iat: Number.MIN_SAFE_INTEGER },
      { exp: now + 3601 },
      { uid: 'b' },
      { sub: 'b' },
      { sid: '' },
      { iat: 1.5 }
    ]) {
      expect(() => admitToken(jwt('a', fields), 'a', now)).toThrow()
    }
  })
})

describe('native secure store admission', () => {
  it('rejects unavailable and Linux plaintext backends, distinguishes corrupt/wrong-identity data, and writes only ciphertext', () => {
    const root = mkdtempSync(join(tmpdir(), 'electron-identity-'))
    const file = join(root, 'identity.bin')
    let available = true,
      backend = 'gnome_libsecret'
    const encryption = {
      isEncryptionAvailable: () => available,
      getSelectedStorageBackend: () => backend,
      encryptString: (value: string) => Buffer.from(value).map((byte) => byte ^ 87),
      decryptString: (value: Buffer) =>
        Buffer.from(value)
          .map((byte) => byte ^ 87)
          .toString()
    }
    try {
      const store = new SecureCredentialStore(file, 'brand.target.local', encryption, 'linux')
      expect(store.read().credential).toBeNull()
      for (const candidate of ['basic_text', 'unknown']) {
        backend = candidate
        expect(() => store.read()).toThrow('Secure storage is unavailable')
      }
      backend = 'gnome_libsecret'
      available = false
      expect(() =>
        store.write({ credential: null, lastUserId: null, clearEpoch: 0, cleanupPending: false })
      ).toThrow()
      available = true
      store.write({
        credential: {
          session: 'opaque-private',
          user: { id: 'a', email: 'a@example.invalid', name: 'a' }
        },
        lastUserId: 'a',
        clearEpoch: 0,
        cleanupPending: false
      })
      expect(readFileSync(file).toString()).not.toContain('opaque-private')
      expect(store.read().credential?.session).toBe('opaque-private')
      expect(() =>
        new SecureCredentialStore(file, 'other.target.local', encryption, 'linux').read()
      ).toThrow('saved credential could not be read')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})
