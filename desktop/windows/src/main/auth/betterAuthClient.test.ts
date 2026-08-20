import { describe, expect, it, vi } from 'vitest'
import { BetterAuthClient, BetterAuthError, jwtSubject } from './betterAuthClient'

function jwt(subject: string, exp = 2_000_000_000): string {
  return `${Buffer.from('{}').toString('base64url')}.${Buffer.from(JSON.stringify({ sub: subject, exp })).toString('base64url')}.sig`
}

describe('BetterAuthClient', () => {
  it('signs in, exchanges the opaque session for a JWT, and keeps the opaque token main-side', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ user: { id: 'u1', email: 'owner@example.test' } }), {
          status: 200,
          headers: { 'set-auth-token': 'opaque-session' }
        })
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ token: jwt('u1') }), { status: 200 }))
    const result = await new BetterAuthClient(
      'https://auth.example.test',
      fetchImpl as typeof fetch
    ).signIn({ email: 'owner@example.test', password: 'secret', createAccount: false })

    expect(result.stored.sessionToken).toBe('opaque-session')
    expect(result.session.user.uid).toBe('u1')
    expect(result.session.token).not.toContain('opaque-session')
    expect(fetchImpl.mock.calls.map(([url]) => (url as URL).pathname)).toEqual([
      '/api/auth/sign-in/email',
      '/api/auth/token'
    ])
  })

  it('rejects a JWT whose subject differs from the database session owner', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ user: { id: 'u1' } }), {
          status: 200,
          headers: { 'set-auth-token': 'opaque-session' }
        })
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ token: jwt('u2') }), { status: 200 }))
    await expect(
      new BetterAuthClient('https://auth.example.test', fetchImpl as typeof fetch).signIn({
        email: 'owner@example.test',
        password: 'secret',
        createAccount: false
      })
    ).rejects.toMatchObject({ definitive: true })
  })

  it('classifies credential rejection as definitive and network failure as transient', async () => {
    const rejected = new BetterAuthClient(
      'https://auth.example.test',
      vi.fn().mockResolvedValue(new Response('{}', { status: 401 })) as typeof fetch
    )
    await expect(
      rejected.signIn({ email: 'x', password: 'bad', createAccount: false })
    ).rejects.toEqual(expect.objectContaining<Partial<BetterAuthError>>({ definitive: true }))

    const offline = new BetterAuthClient(
      'https://auth.example.test',
      vi.fn().mockRejectedValue(new Error('offline')) as typeof fetch
    )
    await expect(
      offline.signIn({ email: 'x', password: 'bad', createAccount: false })
    ).rejects.toEqual(expect.objectContaining<Partial<BetterAuthError>>({ definitive: false }))
  })

  it('parses uid or sub ownership claims only from JWT payloads', () => {
    expect(jwtSubject(jwt('u1'))).toBe('u1')
    expect(jwtSubject('not-a-jwt')).toBeNull()
  })
})
