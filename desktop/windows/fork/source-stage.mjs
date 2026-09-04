import ts from 'typescript'
import { readFileSync, writeFileSync, readdirSync, existsSync, rmSync } from 'node:fs'
import { resolve, relative, dirname, join } from 'node:path'

const root = resolve(process.argv[2])
const profile = JSON.parse(readFileSync(join(root, 'fork/profile.json'), 'utf8'))
const read = (p) => readFileSync(join(root, p), 'utf8')
const rewritten = new Set()
const owners = JSON.parse(read('fork/source-owners.json'))
const generated = new Set(['src/main/application.ts', '.env', 'electron-builder.fork.config.mjs'])
const write = (p, s) => {
  if (!p.startsWith('fork/') && !generated.has(p) && !owners[p])
    throw new Error(`Unreviewed source owner: ${p}`)
  writeFileSync(join(root, p), s)
  rewritten.add(p)
}
function edit(path, patches) {
  let text = read(path)
  for (const { start, end, value } of patches.sort((a, b) => b.start - a.start))
    text = text.slice(0, start) + value + text.slice(end)
  write(path, text)
  rewritten.add(path)
}
function tree(path) {
  return ts.createSourceFile(
    path,
    read(path),
    ts.ScriptTarget.Latest,
    true,
    path.endsWith('tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  )
}
function visit(node, fn) {
  fn(node)
  ts.forEachChild(node, (child) => visit(child, fn))
}
function replaceOnce(path, before, after) {
  const text = read(path)
  const start = text.indexOf(before)
  if (start < 0 || text.indexOf(before, start + before.length) !== -1)
    throw new Error(`Expected one reviewed span: ${path}: ${before.slice(0, 70)}`)
  edit(path, [{ start, end: start + before.length, value: after }])
}
function removeCalls(path, names, byChannel = false) {
  const source = tree(path),
    patches = []
  visit(source, (node) => {
    if (!ts.isExpressionStatement(node) || !ts.isCallExpression(node.expression)) return
    const call = node.expression
    const key =
      byChannel && call.arguments[0] && ts.isStringLiteral(call.arguments[0])
        ? call.arguments[0].text
        : call.expression.getText(source)
    if (names.includes(key))
      patches.push({ start: node.getStart(source), end: node.end, value: '' })
  })
  if (patches.length !== names.length) throw new Error(`Missing exact call owners: ${path}`)
  edit(path, patches)
}
function removeProperties(path, names) {
  const source = tree(path),
    patches = []
  visit(source, (node) => {
    if (
      (!ts.isPropertyAssignment(node) && !ts.isPropertySignature(node)) ||
      !node.name ||
      !names.includes(node.name.getText(source))
    )
      return
    let end = node.end
    if (source.text[end] === ',') end++
    patches.push({ start: node.getStart(source), end, value: '' })
  })
  if (patches.length !== names.length) throw new Error(`Missing exact API properties: ${path}`)
  edit(path, patches)
}
function removeBindings(path, names) {
  const source = tree(path),
    patches = []
  for (const node of source.statements) {
    if (
      !ts.isImportDeclaration(node) ||
      !node.importClause?.namedBindings ||
      !ts.isNamedImports(node.importClause.namedBindings)
    )
      continue
    const kept = node.importClause.namedBindings.elements.filter(
      (item) => !names.includes(item.name.text)
    )
    if (kept.length === node.importClause.namedBindings.elements.length) continue
    const value = kept.length
      ? `import { ${kept.map((item) => item.getText(source)).join(', ')} } from ${node.moduleSpecifier.getText(source)}`
      : ''
    patches.push({ start: node.getStart(source), end: node.end, value })
  }
  edit(path, patches)
}
function removeFunction(path, name) {
  const source = tree(path)
  const node = source.statements.find(
    (node) => ts.isFunctionDeclaration(node) && node.name?.text === name
  )
  if (!node) throw new Error(`Missing function ${path}:${name}`)
  edit(path, [{ start: node.getStart(source), end: node.end, value: '' }])
}
const retiredBridge = [
  'authStore',
  'signInWithProvider',
  'aiProfileSetSession',
  'rewindSetEmbedSession',
  'pimonoSetSession',
  'onSessionTokenRequest',
  'respondSessionToken'
]
removeProperties('src/preload/index.ts', retiredBridge)
removeProperties('src/shared/types.ts', retiredBridge)
replaceOnce(
  'src/shared/types.ts',
  `aiProfileGenerateNow: (session?: {\n    apiBase: string\n    desktopApiBase: string\n    token: string\n  })`,
  'aiProfileGenerateNow: ()'
)
write('src/preload/index.ts', "import '../../fork/native/preload'\n" + read('src/preload/index.ts'))
// The preceding import replacement is deliberately one reviewed first import;
// the whole upstream input hash is checked before any transformation.
removeCalls('src/main/ipc/pimono.ts', ['pimono:setSession'], true)
removeCalls('src/main/ipc/aiUserProfile.ts', ['aiProfile:setSession'], true)
removeCalls('src/main/ipc/rewind.ts', ['rewind:setEmbedSession'], true)
replaceOnce('src/main/ipc/aiUserProfile.ts', 'return generateNow(session)', 'return generateNow()')
// Remove the optional renderer-owned credentials from the remaining generation API.
replaceOnce(
  'src/main/ipc/aiUserProfile.ts',
  `      if (session !== undefined && !isSession(session)) {\n        throw new Error('aiProfile: malformed session')\n      }`,
  ''
)
replaceOnce(
  'src/main/ipc/aiUserProfile.ts',
  '(_e: IpcMainInvokeEvent, session?: unknown)',
  '(_e: IpcMainInvokeEvent)'
)
replaceOnce(
  'src/preload/index.ts',
  `aiProfileGenerateNow: (session?: { apiBase: string; desktopApiBase: string; token: string }) =>\n    ipcRenderer.invoke('aiProfile:generateNow', session)`,
  `aiProfileGenerateNow: () =>\n    ipcRenderer.invoke('aiProfile:generateNow')`
)

const main = 'src/main/index.ts'
removeCalls(main, ['registerAuthStoreHandlers'])
replaceOnce(
  main,
  "import { registerAuthHandlers } from './ipc/auth'",
  "import { registerIdentity, currentBackendSession } from '../../fork/native/runtime'"
)
// Retain the callback's window surfacing only at the window layer; identity
// registration itself has no renderer-provided callback or provider payload.
const source = tree(main),
  patches = []
visit(source, (node) => {
  if (
    ts.isExpressionStatement(node) &&
    ts.isCallExpression(node.expression) &&
    node.expression.expression.getText(source) === 'registerAuthHandlers'
  )
    patches.push({ start: node.getStart(source), end: node.end, value: 'registerIdentity()' })
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'makeRendererTokenRefresher')
    patches.push({ start: node.getStart(source), end: node.end, value: 'currentBackendSession' })
})
if (patches.length !== 2) throw new Error('Main identity lifecycle drift')
edit(main, patches)
replaceOnce(
  main,
  'if (import.meta.env.DEV) devBench.applySandboxUserDataOverride()',
  '// userData is pinned by fork/native/paths before loading this module.'
)
replaceOnce(
  main,
  "electronApp.setAppUserModelId('com.omiwindows.app')",
  `electronApp.setAppUserModelId(${JSON.stringify(profile.applicationId)})`
)
replaceOnce(main, "title: 'omi'", `title: ${JSON.stringify(profile.displayName)}`)
{
  const text = read(main)
  const start = text.indexOf("  // Omi's API doesn't advertise")
  const end = text.indexOf('  // System-audio (loopback)', start)
  if (start < 0 || end < start) throw new Error('Native API CORS owner drift')
  edit(main, [
    {
      start,
      end,
      value: `  installNativeApiAccess(session.defaultSession.webRequest, ${JSON.stringify(profile.apiBase)},
    () => rendererBaseUrl() ?? (process.env.ELECTRON_RENDERER_URL ? new URL(process.env.ELECTRON_RENDERER_URL).origin : null),
    id => { try { const contents = webContents.fromId(id); return contents ? new URL(contents.getURL()).origin : null } catch { return null } })\n\n`
    }
  ])
  write(
    main,
    "import { webContents } from 'electron'\nimport { installNativeApiAccess } from '../../fork/native/apiAccess'\n" +
      read(main)
  )
}

write('src/main/application.ts', read(main))
write(main, `import '../../fork/native/paths'\nimport './application'\n`)

// Updater is explicitly unsupported for this local identity candidate. No feed,
// installer callback, inherited GitHub owner, or OMI_UPDATER_DEV override survives.
write(
  'src/main/updater.ts',
  `import { app } from 'electron'\nimport type { UpdateCheckResult } from '../shared/types'\nexport function getPendingUpdate(): null { return null }\nexport async function checkForUpdatesNow(): Promise<UpdateCheckResult> { return { status: 'unsupported', version: app.getVersion() } }\nexport function installUpdateNow(): boolean { return false }\nexport function initAutoUpdater(): void {}\n`
)
replaceOnce('src/main/application.ts', 'initAutoUpdater(() => mainWindow)', 'initAutoUpdater()')

// Preserve the existing PCM/header WS protocol, route it to the selected API.
const ws = profile.apiBase.replace(/^http/, 'ws').replace(/\/$/, '')
replaceOnce(
  'src/main/ipc/omiListen.ts',
  "'wss://api.omi.me/v2/voice-message/transcribe-stream'",
  JSON.stringify(ws + '/v2/voice-message/transcribe-stream')
)
replaceOnce(
  'src/main/ipc/omiListen.ts',
  "'wss://api.omi.me/v4/listen'",
  JSON.stringify(ws + '/v4/listen')
)

removeCalls('src/renderer/src/lib/appLifetimeJobs.ts', ['maybeStartInsightEngine'])
const retired = [
  'src/renderer/src/components/onboarding/NameStep.tsx',
  'src/renderer/src/components/settings/tabs/AccountTab.tsx',
  'src/renderer/src/lib/insightEngine.ts',
  'src/renderer/src/lib/aiProfileHost.ts',
  'src/renderer/src/lib/rewindEmbedHost.ts',
  'src/renderer/src/lib/piMonoAuthHost.ts',
  'src/renderer/src/lib/firebase.ts',
  'src/renderer/src/lib/authSession.ts',
  'src/renderer/src/hooks/useAuth.ts',
  'src/renderer/src/pages/Login.tsx',
  'src/renderer/src/lib/encryptedAuthPersistence.ts',
  'src/main/ipc/authStore.ts',
  'src/main/ipc/auth.ts',
  'src/main/auth/firebaseIdToken.ts',
  'src/main/assistants/core/tokenPull.ts'
]

// Synchronous renderer projections only. Main owns every asynchronous store
// clear, including cross-account admission, so a late clear cannot erase B.
let teardown = read('src/renderer/src/lib/authTeardown.ts')
const functionStart = teardown.indexOf('export async function teardownUserData()')
const cacheStart = teardown.indexOf('  // 3. In-memory')
const functionEnd = teardown.indexOf('\n}\n', cacheStart) + 3
teardown =
  teardown.slice(0, functionStart) +
  'export function clearRendererUserData(): void {\n' +
  teardown.slice(cacheStart, functionEnd)
write('src/renderer/src/lib/authTeardown.ts', teardown)
rewritten.add('src/renderer/src/lib/authTeardown.ts')

const mapping = new Map([
  ['src/renderer/src/lib/firebase', 'fork/renderer/identity'],
  ['src/renderer/src/lib/authSession', 'fork/renderer/authSession'],
  ['src/renderer/src/hooks/useAuth', 'fork/renderer/useAuth'],
  ['src/renderer/src/pages/Login', 'fork/renderer/Login'],
  ['src/renderer/src/components/onboarding/NameStep', 'fork/renderer/NameStep'],
  ['src/renderer/src/components/settings/tabs/AccountTab', 'fork/renderer/AccountTab']
])
function files(directory) {
  return readdirSync(join(root, directory), { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? files(join(directory, entry.name)) : [join(directory, entry.name)]
  )
}
for (const path of files('src')) {
  if (!/\.(ts|tsx)$/.test(path) || /\.test\.|\.e2e\./.test(path) || retired.includes(path)) continue
  const source = tree(path),
    imports = []
  for (const node of source.statements) {
    if (!ts.isImportDeclaration(node) || !ts.isStringLiteral(node.moduleSpecifier)) continue
    const spec = node.moduleSpecifier.text
    const normalized = relative(root, resolve(root, dirname(path), spec)).replaceAll('\\', '/')
    const target =
      spec === 'firebase/auth' && path.startsWith('src/renderer/')
        ? 'fork/renderer/identity'
        : mapping.get(normalized)
    if (target) {
      let replacement = relative(dirname(path), target).replaceAll('\\', '/')
      if (!replacement.startsWith('.')) replacement = './' + replacement
      imports.push({
        start: node.moduleSpecifier.getStart(source),
        end: node.moduleSpecifier.end,
        value: JSON.stringify(replacement)
      })
    }
  }
  if (imports.length) edit(path, imports)
}
for (const path of retired) {
  if (!owners[path]) throw new Error(`Unreviewed retired owner: ${path}`)
  rmSync(join(root, path))
}

// The old renderer may still be processing an A request while B signs in.
// Pin the actual user object before dispatch; never replay A's 401 as B.
const api = 'src/renderer/src/lib/apiClient.ts'
replaceOnce(
  api,
  '  __retryCount?: number',
  '  __identityOwner?: typeof auth.currentUser\n  __retryCount?: number'
)
replaceOnce(
  api,
  '  const status = error.response?.status',
  '  const status = error.response?.status\n  if (config && config.__identityOwner !== auth.currentUser) return Promise.reject(error)'
)
replaceOnce(
  api,
  "      if (outcome.status === 'ok') {",
  "      if (config.__identityOwner !== auth.currentUser) return Promise.reject(error)\n      if (outcome.status === 'ok') {"
)
replaceOnce(
  api,
  '    const user = auth.currentUser',
  '    const user = auth.currentUser\n    if ((config as RetryConfig).__identityOwner !== undefined && (config as RetryConfig).__identityOwner !== user) throw new Error("Account changed")\n    ;(config as RetryConfig).__identityOwner = user'
)
replaceOnce(
  api,
  '      const token = await user.getIdToken()',
  '      const token = await user.getIdToken()\n      if (auth.currentUser !== user) throw new Error("Account changed")'
)

// Opt out of inherited analytics. The runtime API remains a no-op because
// analytics is explicitly disabled for this local test profile.
write(
  'src/renderer/src/lib/analytics.ts',
  'export function trackEvent(_event: string, _properties: Record<string, unknown> = {}): void {}\nexport function trackHowDidYouHear(_source: string): void {}\n'
)
for (const path of files('src/renderer').filter((p) => p.endsWith('.html'))) {
  let html = read(path).replace(
    /<title>[^<]*<\/title>/,
    `<title>${profile.displayName.replaceAll('&', '&amp;').replaceAll('<', '&lt;')}</title>`
  )
  html = html.replace(
    /connect-src[^;]*;/,
    `connect-src 'self' ${new URL(profile.apiBase).origin} ${new URL(ws).origin};`
  )
  write(path, html)
}

// Whitelist public endpoints; .env.example (upstream production values) is never
// copied. The upstream build prestep sees this explicit file and cannot fallback.
write(
  '.env',
  `VITE_OMI_API_BASE=${profile.apiBase}\nVITE_OMI_DESKTOP_API_BASE=${profile.apiBase}\nVITE_OMI_SHARE_BASE_URL=${profile.shareBase}\n`
)
const publicDefines = {
  'import.meta.env.VITE_OMI_API_BASE': JSON.stringify(profile.apiBase),
  'import.meta.env.VITE_OMI_DESKTOP_API_BASE': JSON.stringify(profile.apiBase),
  'import.meta.env.VITE_OMI_SHARE_BASE_URL': JSON.stringify(profile.shareBase)
}
for (const target of ['main', 'preload', 'renderer'])
  replaceOnce(
    'electron.vite.config.ts',
    `  ${target}: {`,
    `  ${target}: { define: ${JSON.stringify(publicDefines)},`
  )
// Package metadata is a generated local artifact. Dependency versions and lock
// resolution remain exact; production publishing is intentionally unavailable.
const pkg = JSON.parse(read('package.json'))
pkg.name = profile.applicationId.replaceAll('.', '-')
pkg.productName = profile.displayName
pkg.author = 'Local identity fixture'
pkg.homepage = profile.apiBase
for (const [name, script] of Object.entries(pkg.scripts)) {
  if (script.includes('--config electron-builder.config.mjs'))
    pkg.scripts[name] =
      script.replaceAll(
        '--config electron-builder.config.mjs',
        '--config electron-builder.fork.config.mjs'
      ) + (script.includes('--publish never') ? '' : ' --publish never')
}
pkg.scripts['seed:auth'] = 'node -e "throw new Error(\'Session seeding is not supported\')"'
write('package.json', JSON.stringify(pkg, null, 2) + '\n')
write(
  'electron-builder.fork.config.mjs',
  `import base from './electron-builder.config.mjs'\nexport default { ...base, appId: ${JSON.stringify(profile.applicationId)}, productName: ${JSON.stringify(profile.displayName)}, publish: null, win: { ...base.win, executableName: ${JSON.stringify(pkg.name)} }, nsis: { ...base.nsis, artifactName: '${pkg.name}-setup-\${version}.\${ext}' }, linux: { ...base.linux, executableName: ${JSON.stringify(pkg.name)}, maintainer: 'Local identity fixture' }, mac: { ...base.mac, identity: null } }\n`
)

// Stage tests have a dedicated fork runner. Upstream tests remain verbatim and
// execute in the original tree; they do not assert the retired provider shape.
for (const p of ['tsconfig.node.json', 'tsconfig.web.json']) {
  const config = JSON.parse(read(p))
  config.exclude = ['**/*.test.*', '**/*.e2e.*', 'fork/tests/**']
  write(p, JSON.stringify(config, null, 2) + '\n')
}

removeBindings('src/main/application.ts', [
  'registerAuthStoreHandlers',
  'makeRendererTokenRefresher'
])
removeFunction('src/main/ipc/aiUserProfile.ts', 'isSession')
removeBindings('src/main/ipc/aiUserProfile.ts', ['configureAiProfileSession', 'AiProfileSession'])
removeBindings('src/main/ipc/pimono.ts', [
  'configurePiMonoSession',
  'getPiMonoSession',
  'ensurePiMonoAdapterRegistered',
  'setControlPlaneOwner',
  'verifyFirebaseIdToken',
  'clearRendererConversationBinding',
  'fenceRendererConversationOwner'
])
removeBindings('src/main/ipc/rewind.ts', ['configureRewindEmbedSession'])
removeBindings('src/preload/index.ts', ['SignInProvider'])
removeBindings('src/renderer/src/lib/appLifetimeJobs.ts', ['maybeStartInsightEngine'])
removeBindings('src/renderer/src/lib/authTeardown.ts', ['resetOnboarding'])
replaceOnce(
  'src/renderer/src/lib/authTeardown.ts',
  'export function clearRendererUserData(): void {',
  'export function clearRendererUserData(): void {\n  resetByokKeys()'
)

write(
  'src/main/ipc/omiListen.ts',
  "import { identityOwner, currentBackendSession } from '../../../fork/native/runtime'\n" +
    read('src/main/ipc/omiListen.ts')
)
replaceOnce(
  'src/main/ipc/omiListen.ts',
  "ipcMain.handle('omi-listen:start', (e, args: ListenStartArgs) => {",
  "ipcMain.handle('omi-listen:start', async (e, args: ListenStartArgs) => {"
)
replaceOnce(
  'src/main/ipc/omiListen.ts',
  '    startSession(args, e.sender)',
  `    const owner = identityOwner.snapshot().owner
    if (!identityOwner.ownsAccessToken(args.token)) throw new Error('Departed identity')
    const session = await currentBackendSession(false)
    if (!session || identityOwner.snapshot().owner !== owner || !canStartSession(e.sender.id)) throw new Error('Departed identity')
    startSession({ ...args, token: session.token }, e.sender)`
)

replaceOnce(
  'tailwind.config.ts',
  "'./src/renderer/src/**/*.{js,ts,jsx,tsx}'",
  "'./src/renderer/src/**/*.{js,ts,jsx,tsx}', './fork/renderer/**/*.{ts,tsx}'"
)

write(
  'src/renderer/src/pages/Onboarding.tsx',
  "import { auth } from '../../../../fork/renderer/identity'\nimport { OnboardingAccountAction } from '../../../../fork/renderer/OnboardingAccountAction'\n" +
    read('src/renderer/src/pages/Onboarding.tsx')
)
replaceOnce(
  'src/renderer/src/pages/Onboarding.tsx',
  '<div className="app-canvas relative flex h-full">',
  '<div className="app-canvas relative flex h-full"><OnboardingAccountAction />'
)
{
  const path = 'src/renderer/src/pages/Onboarding.tsx',
    source = tree(path),
    patches = []
  visit(source, (node) => {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'handleName') {
      patches.push({
        start: node.initializer.getStart(source),
        end: node.initializer.end,
        value: `async (name: string): Promise<void> => {
    const user = auth.currentUser
    if (!user) throw new Error('Please sign in again.')
    await setDisplayName(name)
    if (auth.currentUser !== user) throw new Error('Account changed')
    await addUserNode(auth.currentUser.displayName)
    if (auth.currentUser !== user) throw new Error('Account changed')
    next()
  }`
      })
    }
  })
  if (patches.length !== 1) throw new Error('Onboarding name owner drift')
  edit(path, patches)
}

write('fork/transformed-paths.json', JSON.stringify([...rewritten].sort(), null, 2) + '\n')
