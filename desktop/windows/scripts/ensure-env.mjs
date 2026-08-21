// Signed build prestep. Cloud builds retain the historical public defaults, while a
// self-hosted release must provide an explicit .env and is rejected before Vite can
// bake an Omi/Firebase/vendor fallback into the artifact.
import { existsSync, copyFileSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const env = join(root, '.env')
const example = join(root, '.env.example')
const requestedProfile =
  process.argv.find((arg) => arg.startsWith('--profile='))?.slice('--profile='.length) ??
  process.env.OMI_DEPLOYMENT_PROFILE

if (!existsSync(env)) {
  if (requestedProfile === 'self_hosted') {
    console.error(
      '[ensure-env] FATAL: self_hosted builds require an explicit .env; copy .env.selfhost.example and set every operator-owned origin.'
    )
    process.exit(1)
  }
  if (!existsSync(example)) {
    console.error(
      '[ensure-env] FATAL: no .env and no .env.example — the build would bake undefined renderer config.'
    )
    process.exit(1)
  }
  copyFileSync(example, env)
  console.log('[ensure-env] no .env found — copied .env.example → .env (omi_cloud defaults).')
}

function parseDotEnv(raw) {
  const values = {}
  for (const line of raw.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/)
    if (!match) continue
    values[match[1]] = match[2].replace(/^(['"])(.*)\1$/, '$2')
  }
  return values
}

const values = parseDotEnv(readFileSync(env, 'utf8'))
const effective = (name) => process.env[name]?.trim() || values[name]?.trim() || ''
const profile = requestedProfile || values.VITE_OMI_DEPLOYMENT_PROFILE || 'omi_cloud'
if (profile !== 'omi_cloud' && profile !== 'self_hosted') {
  console.error(`[ensure-env] FATAL: unsupported deployment profile ${profile}`)
  process.exit(1)
}
if (requestedProfile && values.VITE_OMI_DEPLOYMENT_PROFILE !== requestedProfile) {
  console.error(
    `[ensure-env] FATAL: requested ${requestedProfile}, but .env declares ${values.VITE_OMI_DEPLOYMENT_PROFILE || '(missing)'}`
  )
  process.exit(1)
}

if (profile === 'self_hosted') {
  const required = [
    'VITE_OMI_API_BASE',
    'VITE_OMI_DESKTOP_API_BASE',
    'VITE_OMI_AUTH_BASE',
    'VITE_OMI_MCP_BASE'
  ]
  const missing = required.filter((name) => !effective(name))
  if (effective('VITE_OMI_IDENTITY_PROVIDER') !== 'better_auth') {
    missing.push('VITE_OMI_IDENTITY_PROVIDER=better_auth')
  }
  if (missing.length) {
    console.error(`[ensure-env] FATAL: self_hosted configuration missing: ${missing.join(', ')}`)
    process.exit(1)
  }

  const forbidden = [
    /(^|\.)omi\.me$/i,
    /^desktop-backend-hhibjajaja-uc\.a\.run\.app$/i,
    /^desktop-backend-dt5lrfkkoa-uc\.a\.run\.app$/i,
    /(^|\.)googleapis\.com$/i,
    /(^|\.)firebase(?:app|io)\.com$/i,
    /(^|\.)openai\.com$/i,
    /(^|\.)anthropic\.com$/i,
    /(^|\.)deepgram\.com$/i
  ]
  for (const name of required) {
    let url
    try {
      url = new URL(effective(name))
    } catch {
      console.error(`[ensure-env] FATAL: ${name} must be an absolute HTTPS origin`)
      process.exit(1)
    }
    if (
      url.protocol !== 'https:' ||
      url.pathname !== '/' ||
      url.search ||
      url.hash ||
      url.username ||
      url.password ||
      forbidden.some((pattern) => pattern.test(url.hostname))
    ) {
      console.error(`[ensure-env] FATAL: ${name} must be an operator-owned HTTPS origin`)
      process.exit(1)
    }
  }
  const firebaseNames = new Set([
    ...Object.keys(values).filter((name) => name.startsWith('VITE_FIREBASE_')),
    ...Object.keys(process.env).filter((name) => name.startsWith('VITE_FIREBASE_'))
  ])
  const firebaseValues = [...firebaseNames].filter((name) => effective(name))
  if (firebaseValues.length) {
    console.error(
      '[ensure-env] FATAL: self_hosted artifacts must not contain Firebase configuration'
    )
    process.exit(1)
  }
  if (effective('VITE_SENTRY_DSN') || effective('MAIN_VITE_SENTRY_DSN')) {
    console.error('[ensure-env] FATAL: self_hosted artifacts must not contain a Sentry DSN')
    process.exit(1)
  }
  if (Boolean(effective('VITE_OMI_ANALYTICS_BASE')) !== Boolean(effective('VITE_POSTHOG_KEY'))) {
    console.error(
      '[ensure-env] FATAL: self_hosted analytics needs both VITE_OMI_ANALYTICS_BASE and VITE_POSTHOG_KEY, or neither'
    )
    process.exit(1)
  }
}

console.log(`[ensure-env] validated ${profile} deployment profile.`)
