export type DeploymentProfile = 'omi_cloud' | 'self_hosted'
export type IdentityProvider = 'firebase' | 'better_auth'

export type DeploymentEnvironment = Partial<Record<string, string | undefined>>

export type WindowsDeploymentConfig = {
  profile: DeploymentProfile
  identityProvider: IdentityProvider
  apiBase: string
  desktopApiBase: string
  authBase: string
  mcpBase: string
  shareBase?: string
  analyticsBase?: string
  analyticsKey?: string
  updateFeed?: string
  allowDirectModelProviders: boolean
  allowByok: boolean
  allowCloudConnectors: boolean
}

const OFFICIAL_API = 'https://api.omi.me'
const OFFICIAL_DESKTOP_API = 'https://desktop-backend-hhibjajaja-uc.a.run.app'
const OFFICIAL_SHARE = 'https://h.omi.me'
const OFFICIAL_ANALYTICS = 'https://us.i.posthog.com'

const FORBIDDEN_SELF_HOSTED_HOSTS = new Set([
  'api.omi.me',
  'h.omi.me',
  'desktop-backend-hhibjajaja-uc.a.run.app',
  'identitytoolkit.googleapis.com',
  'securetoken.googleapis.com',
  'firebase.googleapis.com',
  'api.openai.com',
  'api.anthropic.com',
  'generativelanguage.googleapis.com',
  'api.deepgram.com'
])

function isForbiddenSelfHostedHost(rawHostname: string): boolean {
  const hostname = rawHostname.toLowerCase().replace(/\.+$/, '')
  return (
    FORBIDDEN_SELF_HOSTED_HOSTS.has(hostname) ||
    hostname === 'omi.me' ||
    hostname.endsWith('.omi.me') ||
    hostname === 'omiapi.com' ||
    hostname.endsWith('.omiapi.com')
  )
}

function trimmed(env: DeploymentEnvironment, name: string): string | undefined {
  const value = env[name]?.trim()
  return value ? value : undefined
}

function exactProfile(value: string | undefined): DeploymentProfile {
  if (!value || value === 'omi_cloud') return 'omi_cloud'
  if (value === 'self_hosted') return value
  throw new Error(`Unsupported VITE_OMI_DEPLOYMENT_PROFILE: ${value}`)
}

function exactIdentity(value: string | undefined, profile: DeploymentProfile): IdentityProvider {
  const resolved = value ?? (profile === 'self_hosted' ? 'better_auth' : 'firebase')
  if (resolved !== 'firebase' && resolved !== 'better_auth') {
    throw new Error(`Unsupported VITE_OMI_IDENTITY_PROVIDER: ${resolved}`)
  }
  if (profile === 'self_hosted' && resolved !== 'better_auth') {
    throw new Error('The self_hosted Windows profile requires better_auth')
  }
  if (profile === 'omi_cloud' && resolved !== 'firebase') {
    throw new Error('The omi_cloud Windows profile requires firebase')
  }
  return resolved
}

function origin(value: string | undefined, name: string, profile: DeploymentProfile): string {
  if (!value) throw new Error(`${name} is required for ${profile}`)
  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new Error(`${name} must be an absolute URL`)
  }
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error(`${name} must be an origin without credentials, path, query, or fragment`)
  }
  if (profile === 'self_hosted' && url.protocol !== 'https:') {
    throw new Error(`${name} must use HTTPS for self_hosted releases`)
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    throw new Error(`${name} must use HTTP or HTTPS`)
  }
  if (profile === 'self_hosted' && isForbiddenSelfHostedHost(url.hostname)) {
    throw new Error(`${name} points at a forbidden implicit vendor host`)
  }
  return url.origin
}

function optionalOrigin(
  value: string | undefined,
  name: string,
  profile: DeploymentProfile
): string | undefined {
  return value ? origin(value, name, profile) : undefined
}

function optionalEndpoint(
  value: string | undefined,
  name: string,
  profile: DeploymentProfile
): string | undefined {
  if (!value) return undefined
  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new Error(`${name} must be an absolute URL`)
  }
  if (url.username || url.password || url.protocol !== 'https:') {
    throw new Error(`${name} must be an HTTPS URL without embedded credentials`)
  }
  if (profile === 'self_hosted' && isForbiddenSelfHostedHost(url.hostname)) {
    throw new Error(`${name} points at a forbidden implicit vendor host`)
  }
  return url.toString()
}

export function resolveWindowsDeployment(
  env: DeploymentEnvironment = import.meta.env as DeploymentEnvironment
): WindowsDeploymentConfig {
  const profile = exactProfile(trimmed(env, 'VITE_OMI_DEPLOYMENT_PROFILE'))
  const identityProvider = exactIdentity(trimmed(env, 'VITE_OMI_IDENTITY_PROVIDER'), profile)

  const apiBase = origin(
    trimmed(env, 'VITE_OMI_API_BASE') ?? (profile === 'omi_cloud' ? OFFICIAL_API : undefined),
    'VITE_OMI_API_BASE',
    profile
  )
  const desktopApiBase = origin(
    trimmed(env, 'VITE_OMI_DESKTOP_API_BASE') ??
      (profile === 'omi_cloud' ? OFFICIAL_DESKTOP_API : undefined),
    'VITE_OMI_DESKTOP_API_BASE',
    profile
  )
  const authBase = origin(
    trimmed(env, 'VITE_OMI_AUTH_BASE') ?? (profile === 'omi_cloud' ? apiBase : undefined),
    'VITE_OMI_AUTH_BASE',
    profile
  )
  const mcpBase = origin(
    trimmed(env, 'VITE_OMI_MCP_BASE') ?? (profile === 'omi_cloud' ? apiBase : undefined),
    'VITE_OMI_MCP_BASE',
    profile
  )

  const shareBase = optionalOrigin(
    trimmed(env, 'VITE_OMI_SHARE_BASE_URL') ?? (profile === 'omi_cloud' ? OFFICIAL_SHARE : undefined),
    'VITE_OMI_SHARE_BASE_URL',
    profile
  )
  const analyticsBase = optionalOrigin(
    trimmed(env, 'VITE_OMI_ANALYTICS_BASE') ??
      (profile === 'omi_cloud' ? OFFICIAL_ANALYTICS : undefined),
    'VITE_OMI_ANALYTICS_BASE',
    profile
  )
  const analyticsKey = trimmed(env, 'VITE_POSTHOG_KEY')
  if (profile === 'self_hosted' && Boolean(analyticsBase) !== Boolean(analyticsKey)) {
    throw new Error('Self-hosted analytics requires both VITE_OMI_ANALYTICS_BASE and VITE_POSTHOG_KEY')
  }

  return {
    profile,
    identityProvider,
    apiBase,
    desktopApiBase,
    authBase,
    mcpBase,
    shareBase,
    analyticsBase,
    analyticsKey,
    updateFeed: optionalEndpoint(
      trimmed(env, 'VITE_OMI_UPDATE_FEED_URL'),
      'VITE_OMI_UPDATE_FEED_URL',
      profile
    ),
    allowDirectModelProviders: profile === 'omi_cloud',
    allowByok: profile === 'omi_cloud',
    allowCloudConnectors: profile === 'omi_cloud'
  }
}

export function backendWebSocketOrigin(config = resolveWindowsDeployment()): string {
  const url = new URL(config.apiBase)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.origin
}

/** Network origins whose CORS response headers may be normalized by Electron.
 * Keep this derived from the signed deployment profile: a self-hosted package
 * must never retain the managed-cloud/PostHog allowlist from the cloud artifact. */
export function rendererCorsUrlPatterns(config = resolveWindowsDeployment()): string[] {
  const origins = [config.apiBase, config.desktopApiBase, config.analyticsBase].filter(
    (value): value is string => Boolean(value)
  )
  return [...new Set(origins)].map((value) => `${value}/*`)
}

/** Crash reporting is a managed-cloud facility. A self-hosted artifact ignores
 * inherited/baked DSNs even if a caller bypassed the build-time environment gate. */
export function deploymentTelemetryDsn(
  dsn: string | undefined,
  config = resolveWindowsDeployment()
): string | undefined {
  if (config.profile === 'self_hosted') return undefined
  const value = dsn?.trim()
  return value || undefined
}

export function requireSelfHostedBackendModelCapability(
  feature: string,
  config = resolveWindowsDeployment()
): void {
  if (config.profile === 'self_hosted') {
    throw new Error(`${feature} is unavailable until the configured backend advertises that capability`)
  }
}
