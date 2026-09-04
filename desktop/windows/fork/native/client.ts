import { IdentityError, type IdentityUser } from './contract'

export interface Credential {
  session: string
  user: IdentityUser
}
export interface AccessToken {
  token: string
  exp: number
  sid: string
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new IdentityError('invalid_response')
  return value as Record<string, unknown>
}
export function parseUser(value: unknown): IdentityUser {
  const u = object(value)
  if (
    typeof u.id !== 'string' ||
    !u.id ||
    typeof u.email !== 'string' ||
    !u.email ||
    typeof u.name !== 'string'
  ) {
    throw new IdentityError('invalid_response')
  }
  return { id: u.id, email: u.email, name: u.name }
}

// Cache admission only. API/WS perform the authoritative signature, issuer,
// audience and live-session verification (contracts/auth/client-cache-admission).
export function admitToken(token: unknown, uid: string, now: number): AccessToken {
  try {
    if (typeof token !== 'string' || token.split('.').length !== 3) throw new Error()
    const p = object(JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8')))
    const { sub, uid: subject, iat, exp, sid } = p
    if (
      sub !== uid ||
      subject !== uid ||
      typeof sid !== 'string' ||
      !sid ||
      !Number.isSafeInteger(iat) ||
      !Number.isSafeInteger(exp) ||
      (iat as number) < 0 ||
      (iat as number) > now + 60 ||
      (exp as number) <= now ||
      (exp as number) <= (iat as number) ||
      (exp as number) - (iat as number) > 3600
    )
      throw new Error()
    return { token, exp: exp as number, sid }
  } catch {
    throw new IdentityError('invalid_response')
  }
}

export class IdentityClient {
  constructor(
    readonly origin: string,
    private fetchImpl: typeof fetch = fetch,
    private now = () => Date.now() / 1000
  ) {}

  private async request(
    path: string,
    method: string,
    session?: string,
    body?: unknown,
    signal?: AbortSignal
  ): Promise<{ response: Response; body: unknown }> {
    let response: Response
    try {
      response = await this.fetchImpl(`${this.origin}/api/auth/${path}`, {
        method,
        redirect: 'error',
        signal: signal
          ? AbortSignal.any([signal, AbortSignal.timeout(12_000)])
          : AbortSignal.timeout(12_000),
        headers: {
          ...(session ? { Authorization: `Bearer ${session}` } : {}),
          ...(body ? { 'Content-Type': 'application/json' } : {})
        },
        ...(body ? { body: JSON.stringify(body) } : {})
      })
    } catch {
      throw new IdentityError(signal?.aborted ? 'superseded' : 'unavailable')
    }
    if (signal?.aborted) throw new IdentityError('superseded')
    if (response.status === 401)
      throw new IdentityError(session ? 'session_expired' : 'invalid_credentials')
    if (response.status >= 500 || response.status === 429) throw new IdentityError('unavailable')
    if (!response.ok) throw new IdentityError(session ? 'invalid_response' : 'invalid_credentials')
    try {
      return { response, body: await response.json() }
    } catch {
      throw new IdentityError('invalid_response')
    }
  }

  async authenticate(
    input: { email: string; password: string; name?: string },
    signal?: AbortSignal
  ): Promise<Credential> {
    if (
      !input ||
      typeof input.email !== 'string' ||
      !input.email.includes('@') ||
      typeof input.password !== 'string' ||
      !input.password ||
      input.password.length > 1024 ||
      (input.name !== undefined && (typeof input.name !== 'string' || !input.name.trim()))
    )
      throw new IdentityError('invalid_input')
    const { response, body } = await this.request(
      input.name === undefined ? 'sign-in/email' : 'sign-up/email',
      'POST',
      undefined,
      input,
      signal
    )
    const session = response.headers.get('set-auth-token')
    if (!session || /[\r\n]/.test(session)) throw new IdentityError('invalid_response')
    return { session, user: parseUser(object(body).user) }
  }
  async restore(session: string, signal?: AbortSignal): Promise<IdentityUser | null> {
    const { body } = await this.request('get-session', 'GET', session, undefined, signal)
    if (body === null) return null
    return parseUser(object(body).user)
  }
  async token(credential: Credential, signal?: AbortSignal): Promise<AccessToken> {
    const { body } = await this.request('token', 'GET', credential.session, undefined, signal)
    return admitToken(object(body).token, credential.user.id, this.now())
  }
  async updateName(
    credential: Credential,
    name: string,
    signal?: AbortSignal
  ): Promise<IdentityUser> {
    if (typeof name !== 'string' || !name.trim() || name.length > 200)
      throw new IdentityError('invalid_input')
    await this.request('update-user', 'POST', credential.session, { name: name.trim() }, signal)
    const user = await this.restore(credential.session, signal)
    if (!user || user.id !== credential.user.id) throw new IdentityError('invalid_response')
    return user
  }
  async signOut(session: string, signal?: AbortSignal): Promise<void> {
    try {
      await this.request('sign-out', 'POST', session, {}, signal)
    } catch (error) {
      if (!(error instanceof IdentityError) || error.code !== 'session_expired') throw error
    }
  }
}
