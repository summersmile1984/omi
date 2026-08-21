import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { extractFile, listPackage, statFile } from '@electron/asar'

const FORBIDDEN = [
  'api.omi.me',
  'h.omi.me',
  'desktop-backend-hhibjajaja-uc.a.run.app',
  'desktop-backend-dt5lrfkkoa-uc.a.run.app',
  'identitytoolkit.googleapis.com',
  'securetoken.googleapis.com',
  'firebase.googleapis.com',
  'firebaseapp.com',
  'firebaseio.com',
  'api.openai.com',
  'api.anthropic.com',
  'generativelanguage.googleapis.com',
  'api.deepgram.com',
  'us.i.posthog.com',
  'sentry.io',
  'cdn.jsdelivr.net'
]

export function artifactHtmlErrors(html, expectedOrigins) {
  const lower = html.toLowerCase()
  const errors = FORBIDDEN.filter((host) => lower.includes(host)).map(
    (host) => `self-hosted renderer artifact contains forbidden CSP/vendor host: ${host}`
  )
  const uncommented = html.replace(/<!--[\s\S]*?-->/g, '')
  const cspTags = (uncommented.match(/<meta\b[^>]*>/gi) ?? []).filter((tag) =>
    /\bhttp-equiv\s*=\s*["']Content-Security-Policy["']/i.test(tag)
  )
  if (cspTags.length !== 1) {
    errors.push(
      `self-hosted renderer must contain exactly one active CSP meta element; found ${cspTags.length}`
    )
    return errors
  }
  const content = cspTags[0].match(/\bcontent\s*=\s*(["'])([\s\S]*?)\1/i)?.[2]
  if (!content) {
    errors.push('self-hosted renderer CSP meta element is missing its content attribute')
    return errors
  }

  const connectDirectives = content
    .split(';')
    .map((directive) => directive.trim().split(/\s+/).filter(Boolean))
    .filter((tokens) => tokens[0]?.toLowerCase() === 'connect-src')
  if (connectDirectives.length !== 1) {
    errors.push(
      `self-hosted renderer CSP must contain exactly one connect-src directive; found ${connectDirectives.length}`
    )
    return errors
  }

  const actualTokens = connectDirectives[0].slice(1)
  const duplicateTokens = actualTokens.filter(
    (token, index) => actualTokens.indexOf(token) !== index
  )
  if (duplicateTokens.length) {
    errors.push(
      `self-hosted renderer CSP connect-src contains duplicate source(s): ${[...new Set(duplicateTokens)].join(', ')}`
    )
  }
  const actual = new Set(actualTokens)
  const expected = new Set(["'self'", ...expectedOrigins.map((origin) => new URL(origin).origin)])
  for (const token of expected) {
    if (!actual.has(token)) {
      errors.push(`self-hosted renderer CSP connect-src is missing signed source: ${token}`)
    }
  }
  for (const token of actual) {
    if (!expected.has(token)) {
      errors.push(`self-hosted renderer CSP connect-src contains unsigned source: ${token}`)
    }
  }
  return errors
}

function parseDotEnv(raw) {
  return Object.fromEntries(
    raw
      .split(/\r?\n/)
      .map((line) => line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/))
      .filter(Boolean)
      .map((match) => [match[1], match[2].replace(/^(['"])(.*)\1$/, '$2')])
  )
}

function htmlFiles(root) {
  const files = []
  for (const name of readdirSync(root)) {
    const path = join(root, name)
    if (statSync(path).isDirectory()) files.push(...htmlFiles(path))
    else if (name.endsWith('.html')) files.push(path)
  }
  return files
}

function filesRecursively(root) {
  const files = []
  for (const name of readdirSync(root)) {
    const path = join(root, name)
    if (statSync(path).isDirectory()) files.push(...filesRecursively(path))
    else files.push(path)
  }
  return files
}

export function packagedArtifactErrors(packagedRoot, env) {
  const asar = join(packagedRoot, 'resources', 'app.asar')
  try {
    if (!statSync(asar).isFile()) return [`final packaged app is missing ${asar}`]
  } catch {
    return [`final packaged app is missing ${asar}`]
  }
  const textFiles = []
  try {
    for (const archivedPath of listPackage(asar, { isPack: false })) {
      if (!/\.(?:html|css|js|mjs|cjs|json)$/i.test(archivedPath)) continue
      const normalizedPath = archivedPath.replace(/^\/+/, '')
      const metadata = statFile(asar, normalizedPath, false)
      if (!metadata || !Object.hasOwn(metadata, 'size')) continue
      textFiles.push({
        path: `app.asar/${normalizedPath}`,
        text: extractFile(asar, normalizedPath).toString('utf8')
      })
    }
  } catch (error) {
    return [
      `final packaged app.asar cannot be inspected: ${error instanceof Error ? error.message : 'invalid archive'}`
    ]
  }
  const unpacked = join(packagedRoot, 'resources', 'app.asar.unpacked')
  try {
    if (statSync(unpacked).isDirectory()) {
      for (const path of filesRecursively(unpacked)) {
        if (/\.(?:html|css|js|mjs|cjs|json)$/i.test(path)) {
          textFiles.push({ path, text: readFileSync(path, 'utf8') })
        }
      }
    }
  } catch {
    // An artifact with no unpacked payload is valid; app.asar remains mandatory.
  }
  const packedText = textFiles.map((file) => file.text).join('\n')
  const errors = []
  if (!packedText.includes('self_hosted')) {
    errors.push('final packaged app does not contain the signed self_hosted profile')
  }
  for (const name of [
    'VITE_OMI_API_BASE',
    'VITE_OMI_DESKTOP_API_BASE',
    'VITE_OMI_AUTH_BASE',
    'VITE_OMI_MCP_BASE'
  ]) {
    if (!env[name]) {
      errors.push(`final packaged artifact gate requires signed ${name}`)
    } else if (!packedText.includes(env[name])) {
      errors.push(`final packaged app is missing signed operator origin ${name}`)
    }
  }
  for (const name of [
    'VITE_OMI_MCP_CHATGPT_OAUTH_CLIENT_ID',
    'VITE_OMI_MCP_CLAUDE_OAUTH_CLIENT_ID'
  ]) {
    if (env[name] && !packedText.includes(env[name])) {
      errors.push(`final packaged app is missing signed public MCP OAuth client ${name}`)
    }
  }
  const origins = [
    env.VITE_OMI_API_BASE,
    env.VITE_OMI_DESKTOP_API_BASE,
    env.VITE_OMI_AUTH_BASE,
    env.VITE_OMI_MCP_BASE,
    env.VITE_OMI_ANALYTICS_BASE
  ].filter(Boolean)
  if (env.VITE_OMI_API_BASE) {
    const websocketOrigin = new URL(env.VITE_OMI_API_BASE)
    websocketOrigin.protocol = 'wss:'
    origins.push(websocketOrigin.origin)
  }
  const rendererHtml = textFiles.filter((candidate) =>
    /(?:^|\/)out\/renderer\/[^/]+\.html$/i.test(candidate.path)
  )
  if (!rendererHtml.length) errors.push('final packaged app contains no emitted renderer HTML')
  for (const file of rendererHtml) {
    errors.push(...artifactHtmlErrors(file.text, origins).map((error) => `${file.path}: ${error}`))
  }
  const credentialPatterns = [
    [/AIza[0-9A-Za-z_-]{30,}/, 'populated Firebase API key'],
    [/https:\/\/[^\s"']+@[^\s"']*sentry[^\s"']*/i, 'populated Sentry DSN'],
    [/\bphc_[0-9A-Za-z_-]{12,}\b/, 'populated PostHog project key']
  ]
  for (const [pattern, label] of credentialPatterns) {
    if (pattern.test(packedText)) errors.push(`final packaged app contains ${label}`)
  }
  return errors
}

export function checkArtifact(outputRoot, envFile) {
  const env = parseDotEnv(readFileSync(envFile, 'utf8'))
  if (env.VITE_OMI_DEPLOYMENT_PROFILE !== 'self_hosted') {
    return ['artifact checker requires VITE_OMI_DEPLOYMENT_PROFILE=self_hosted']
  }
  const origins = [
    env.VITE_OMI_API_BASE,
    env.VITE_OMI_DESKTOP_API_BASE,
    env.VITE_OMI_AUTH_BASE,
    env.VITE_OMI_MCP_BASE,
    env.VITE_OMI_ANALYTICS_BASE
  ].filter(Boolean)
  const api = new URL(env.VITE_OMI_API_BASE)
  api.protocol = 'wss:'
  origins.push(api.origin)

  const renderer = join(outputRoot, 'renderer')
  const files = htmlFiles(renderer)
  if (!files.length) return [`no renderer HTML found under ${renderer}`]
  return files.flatMap((path) =>
    artifactHtmlErrors(readFileSync(path, 'utf8'), origins).map((e) => `${path}: ${e}`)
  )
}

export function checkPackagedArtifact(packagedRoot, envFile) {
  const env = parseDotEnv(readFileSync(envFile, 'utf8'))
  if (env.VITE_OMI_DEPLOYMENT_PROFILE !== 'self_hosted') {
    return ['artifact checker requires VITE_OMI_DEPLOYMENT_PROFILE=self_hosted']
  }
  return packagedArtifactErrors(packagedRoot, env)
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
  const target = resolve(root, process.argv[2] ?? 'out')
  const packagedIndex = process.argv.indexOf('--packaged')
  const errors =
    packagedIndex >= 0
      ? checkPackagedArtifact(resolve(root, process.argv[packagedIndex + 1]), join(root, '.env'))
      : checkArtifact(target, join(root, '.env'))
  if (errors.length) {
    for (const error of errors) console.error(`[self-host-artifact] ${error}`)
    process.exitCode = 1
  } else {
    console.log(
      packagedIndex >= 0
        ? '[self-host-artifact] final packaged payload carries the signed profile without populated vendor credentials'
        : '[self-host-artifact] renderer connect-src exactly matches signed operator origins'
    )
  }
}
