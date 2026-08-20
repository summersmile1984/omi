import { createServer } from 'node:http'
import { generateKeyPair, exportJWK, SignJWT } from 'jose'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { resolveWindowsDeployment } from '../../shared/deploymentProfile'
import { __resetIdentityJwksForTests, verifyIdentityToken } from './identityToken'

const selfHosted = resolveWindowsDeployment({
  VITE_OMI_DEPLOYMENT_PROFILE: 'self_hosted',
  VITE_OMI_IDENTITY_PROVIDER: 'better_auth',
  VITE_OMI_API_BASE: 'https://api.example.test',
  VITE_OMI_DESKTOP_API_BASE: 'https://desktop.example.test',
  VITE_OMI_AUTH_BASE: 'https://auth.example.test',
  VITE_OMI_MCP_BASE: 'https://mcp.example.test'
})

describe('verifyIdentityToken', () => {
  afterEach(() => __resetIdentityJwksForTests())

  it('verifies the production ES256 Better Auth JWKS contract and rejects another algorithm', async () => {
    const es = await generateKeyPair('ES256')
    const esJwk = { ...(await exportJWK(es.publicKey)), kid: 'production-es256', alg: 'ES256', use: 'sig' }
    const ed = await generateKeyPair('EdDSA')
    const server = createServer((_request, response) => {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ keys: [esJwk] }))
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    const address = server.address()
    if (!address || typeof address === 'string') throw new Error('test JWKS server did not bind')
    const issuer = `http://127.0.0.1:${address.port}`
    const config = { ...selfHosted, authBase: issuer }
    try {
      const valid = await new SignJWT({ uid: 'self-host-user' })
        .setProtectedHeader({ alg: 'ES256', kid: 'production-es256' })
        .setIssuer(issuer)
        .setAudience(issuer)
        .setSubject('self-host-user')
        .setExpirationTime('5m')
        .sign(es.privateKey)
      await expect(verifyIdentityToken(valid, { config })).resolves.toBe('self-host-user')

      const wrongAlgorithm = await new SignJWT({ uid: 'self-host-user' })
        .setProtectedHeader({ alg: 'EdDSA', kid: 'production-es256' })
        .setIssuer(issuer)
        .setAudience(issuer)
        .setExpirationTime('5m')
        .sign(ed.privateKey)
      await expect(verifyIdentityToken(wrongAlgorithm, { config })).resolves.toBeNull()
    } finally {
      await new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve()))
      )
    }
  })

  it('uses only configured Better Auth verification for self-hosted tokens', async () => {
    const firebase = vi.fn().mockResolvedValue('google-user')
    const betterAuth = vi.fn().mockResolvedValue('self-host-user')
    await expect(
      verifyIdentityToken('jwt', {
        config: selfHosted,
        verifyFirebase: firebase,
        verifyBetterAuth: betterAuth
      })
    ).resolves.toBe('self-host-user')
    expect(firebase).not.toHaveBeenCalled()
    expect(betterAuth).toHaveBeenCalledWith('jwt', selfHosted)
  })

  it('does not fall back to Firebase when Better Auth rejects a token', async () => {
    const firebase = vi.fn().mockResolvedValue('google-user')
    await expect(
      verifyIdentityToken('forged', {
        config: selfHosted,
        verifyFirebase: firebase,
        verifyBetterAuth: vi.fn().mockResolvedValue(null)
      })
    ).resolves.toBeNull()
    expect(firebase).not.toHaveBeenCalled()
  })
})
