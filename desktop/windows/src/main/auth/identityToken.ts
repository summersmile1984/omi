// Main-process identity verification selected by the signed deployment profile.
// There is no decode or Google fallback: cloud verifies Firebase, self-hosted
// verifies the configured Better Auth JWKS/issuer/audience.
import { createRemoteJWKSet, jwtVerify } from 'jose'
import { resolveWindowsDeployment, type WindowsDeploymentConfig } from '../../shared/deploymentProfile'
import { verifyFirebaseIdToken } from './firebaseIdToken'

type VerifierDependencies = {
  config?: WindowsDeploymentConfig
  verifyFirebase?: (token: string) => Promise<string | null>
  verifyBetterAuth?: (token: string, config: WindowsDeploymentConfig) => Promise<string | null>
}

const remoteJwks = new Map<string, ReturnType<typeof createRemoteJWKSet>>()

async function verifyBetterAuthToken(
  token: string,
  config: WindowsDeploymentConfig
): Promise<string | null> {
  const jwksUrl = new URL('/api/auth/jwks', config.authBase).toString()
  let jwks = remoteJwks.get(jwksUrl)
  if (!jwks) {
    jwks = createRemoteJWKSet(new URL(jwksUrl), {
      timeoutDuration: 10_000,
      cooldownDuration: 30_000,
      cacheMaxAge: 10 * 60_000
    })
    remoteJwks.set(jwksUrl, jwks)
  }
  try {
    const { payload } = await jwtVerify(token, jwks, {
      algorithms: ['ES256'],
      issuer: config.authBase,
      audience: config.authBase
    })
    const uid = typeof payload.uid === 'string' && payload.uid ? payload.uid : payload.sub
    return typeof uid === 'string' && uid ? uid : null
  } catch {
    return null
  }
}

export async function verifyIdentityToken(
  token: string,
  dependencies: VerifierDependencies = {}
): Promise<string | null> {
  const config = dependencies.config ?? resolveWindowsDeployment()
  if (config.identityProvider === 'firebase') {
    return (dependencies.verifyFirebase ?? verifyFirebaseIdToken)(token)
  }
  return (dependencies.verifyBetterAuth ?? verifyBetterAuthToken)(token, config)
}

export function __resetIdentityJwksForTests(): void {
  remoteJwks.clear()
}
