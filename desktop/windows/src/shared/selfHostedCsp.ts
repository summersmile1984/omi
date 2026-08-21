type SelfHostedCspEnvironment = Record<string, string | undefined>

const CSP_META_PATTERN =
  /<meta\b(?=[^>]*\bhttp-equiv\s*=\s*["']Content-Security-Policy["'])[^>]*>/gi

function configuredOrigin(
  env: SelfHostedCspEnvironment,
  name: string,
  required: boolean
): string | null {
  const raw = env[name]?.trim()
  if (!raw) {
    if (required) throw new Error(`self-hosted CSP requires ${name}`)
    return null
  }
  const url = new URL(raw)
  if (
    url.protocol !== 'https:' ||
    url.pathname !== '/' ||
    url.search ||
    url.hash ||
    url.username ||
    url.password
  ) {
    throw new Error(`self-hosted CSP requires ${name} to be an HTTPS origin`)
  }
  return url.origin
}

/** Replaces the one authoritative CSP meta element as a whole. Matching the
 * entire element is deliberate: directive-shaped prose in an adjacent HTML
 * comment must never become a replacement boundary and comment out the CSP. */
export function rewriteSelfHostedCsp(html: string, env: SelfHostedCspEnvironment): string {
  const matches = html.match(CSP_META_PATTERN) ?? []
  if (matches.length !== 1) {
    throw new Error(
      `self-hosted renderer requires exactly one CSP meta element; found ${matches.length}`
    )
  }
  const configured = [
    configuredOrigin(env, 'VITE_OMI_API_BASE', true),
    configuredOrigin(env, 'VITE_OMI_DESKTOP_API_BASE', true),
    configuredOrigin(env, 'VITE_OMI_AUTH_BASE', true),
    configuredOrigin(env, 'VITE_OMI_MCP_BASE', true),
    configuredOrigin(env, 'VITE_OMI_ANALYTICS_BASE', false)
  ].filter((value): value is string => value !== null)
  const websocketApi = new URL(configured[0])
  websocketApi.protocol = 'wss:'
  const connect = [...new Set(["'self'", ...configured, websocketApi.origin])].join(' ')
  const policy = [
    "default-src 'self'",
    "script-src 'self' 'unsafe-eval' 'wasm-unsafe-eval' blob:",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "media-src 'self' blob:",
    `connect-src ${connect}`,
    "frame-src 'self'",
    "worker-src 'self' blob:"
  ].join('; ')
  const meta = `<meta http-equiv="Content-Security-Policy" content="${policy};" />`
  return html.replace(CSP_META_PATTERN, meta)
}
