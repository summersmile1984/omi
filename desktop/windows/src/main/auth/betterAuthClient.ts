import type { BetterAuthSession } from '../../shared/types'

export type BetterAuthStoredSession = {
  sessionToken: string
  userId: string
  email?: string
  displayName?: string
}

export class BetterAuthError extends Error {
  constructor(
    message: string,
    readonly definitive: boolean
  ) {
    super(message)
    this.name = 'BetterAuthError'
  }
}

type FetchLike = typeof fetch

type UserEnvelope = { id?: unknown; email?: unknown; name?: unknown }

function payload(token: string): Record<string, unknown> | null {
  const parts = token.split('.')
  if (parts.length !== 3) return null
  try {
    return JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8')) as Record<string, unknown>
  } catch {
    return null
  }
}

export function jwtSubject(token: string): string | null {
  const body = payload(token)
  const uid = body?.uid
  const sub = body?.sub
  return typeof uid === 'string' && uid ? uid : typeof sub === 'string' && sub ? sub : null
}

function jwtExpiry(token: string): number {
  const exp = payload(token)?.exp
  return typeof exp === 'number' && Number.isFinite(exp) ? exp * 1000 : Date.now() + 15 * 60_000
}

function validUser(raw: unknown): { id: string; email?: string; name?: string } {
  const user = raw as UserEnvelope
  if (!user || typeof user.id !== 'string' || !user.id) {
    throw new BetterAuthError('The authentication service returned an unreadable user.', true)
  }
  return {
    id: user.id,
    email: typeof user.email === 'string' ? user.email : undefined,
    name: typeof user.name === 'string' ? user.name : undefined
  }
}

async function responseJson(response: Response): Promise<Record<string, unknown>> {
  try {
    return (await response.json()) as Record<string, unknown>
  } catch {
    throw new BetterAuthError('The authentication service returned an unreadable response.', false)
  }
}

function requireSuccess(response: Response, credentialRequest: boolean): void {
  if (response.ok) return
  const definitive = response.status === 400 || response.status === 401 || response.status === 403
  const message = credentialRequest && definitive ? 'The email or password was not accepted.' :
    definitive ? 'Your self-hosted session expired. Sign in again.' :
      `The authentication service is unavailable (HTTP ${response.status}).`
  throw new BetterAuthError(message, definitive)
}

export class BetterAuthClient {
  constructor(
    private readonly baseUrl: string,
    private readonly fetchImpl: FetchLike = globalThis.fetch
  ) {}

  async signIn(request: {
    email: string
    password: string
    createAccount: boolean
    name?: string
  }): Promise<{ session: BetterAuthSession; stored: BetterAuthStoredSession }> {
    const path = request.createAccount ? '/api/auth/sign-up/email' : '/api/auth/sign-in/email'
    const body: Record<string, string> = { email: request.email, password: request.password }
    if (request.createAccount) body.name = request.name ?? ''
    const response = await this.call(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
    requireSuccess(response, true)
    const sessionToken = response.headers.get('set-auth-token')
    if (!sessionToken) {
      throw new BetterAuthError('The authentication service did not return a session token.', true)
    }
    const envelope = await responseJson(response)
    const user = validUser(envelope.user)
    return this.exchange({
      sessionToken,
      userId: user.id,
      email: user.email,
      displayName: user.name
    })
  }

  async refresh(stored: BetterAuthStoredSession): Promise<{
    session: BetterAuthSession
    stored: BetterAuthStoredSession
  }> {
    const response = await this.call('/api/auth/get-session', {
      headers: { Authorization: `Bearer ${stored.sessionToken}` }
    })
    requireSuccess(response, false)
    const envelope = await responseJson(response)
    const user = validUser(envelope.user)
    if (user.id !== stored.userId) {
      throw new BetterAuthError('The restored session belongs to a different account.', true)
    }
    return this.exchange({
      ...stored,
      email: user.email ?? stored.email,
      displayName: user.name ?? stored.displayName
    })
  }

  async signOut(sessionToken: string): Promise<void> {
    const response = await this.call('/api/auth/sign-out', {
      method: 'POST',
      headers: { Authorization: `Bearer ${sessionToken}`, 'Content-Type': 'application/json' },
      body: '{}'
    })
    requireSuccess(response, false)
  }

  private async exchange(stored: BetterAuthStoredSession): Promise<{
    session: BetterAuthSession
    stored: BetterAuthStoredSession
  }> {
    const response = await this.call('/api/auth/token', {
      headers: { Authorization: `Bearer ${stored.sessionToken}` }
    })
    requireSuccess(response, false)
    const envelope = await responseJson(response)
    const token = envelope.token
    if (typeof token !== 'string' || jwtSubject(token) !== stored.userId) {
      throw new BetterAuthError('The authentication JWT does not match its session owner.', true)
    }
    return {
      stored,
      session: {
        user: { uid: stored.userId, email: stored.email, displayName: stored.displayName },
        token,
        expiresAt: jwtExpiry(token)
      }
    }
  }

  private async call(path: string, init: RequestInit): Promise<Response> {
    try {
      return await this.fetchImpl(new URL(path, this.baseUrl), {
        ...init,
        headers: { Accept: 'application/json', 'User-Agent': 'omi-windows/1.0', ...init.headers },
        signal: AbortSignal.timeout(15_000)
      })
    } catch (error) {
      if (error instanceof BetterAuthError) throw error
      throw new BetterAuthError('Could not reach the configured authentication service.', false)
    }
  }
}
