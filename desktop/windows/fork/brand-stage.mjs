import ts from 'typescript'

// A reviewed presentation catalogue, not a global rename. Protocol/storage keys
// remain intact. Whole upstream file hashes and exact AST counts both gate drift.
export function applyBrandPresentation(root, profile, { read, edit, write, replaceOnce }) {
  const catalogue = JSON.parse(read('fork/brand-text.json'))
  const values = {
    product: profile.displayName,
    persona: profile.personaName,
    docs: profile.links.docs
  }
  const report = {
    rendered: [],
    preserved: [],
    scope:
      'Electron shell/onboarding/General/Privacy/About/tray text; assets and other pages excluded'
  }
  for (const [path, entries] of Object.entries(catalogue)) {
    const source = ts.createSourceFile(
      path,
      read(path),
      ts.ScriptTarget.Latest,
      true,
      path.endsWith('tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
    )
    const found = entries.map(() => 0),
      patches = []
    function visit(node) {
      const dynamic =
        ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)
      if (
        ts.isStringLiteral(node) ||
        ts.isJsxText(node) ||
        ts.isNoSubstitutionTemplateLiteral(node) ||
        dynamic
      ) {
        const index = entries.findIndex(
          (entry) => entry.kind === ts.SyntaxKind[node.kind] && entry.source === node.text
        )
        if (index < 0 && /\bomi\b/i.test(node.text))
          throw new Error(`Unclassified brand text: ${path}: ${node.text.slice(0, 80)}`)
        if (index >= 0) {
          const entry = entries[index]
          found[index]++
          if (entry.template !== undefined) {
            if (dynamic)
              throw new Error(`Dynamic brand text requires an explicit owner migration: ${path}`)
            const value = entry.template.replace(
              /\{(product|persona|docs)\}/g,
              (_, key) => values[key]
            )
            const literal = JSON.stringify(value)
            patches.push({
              start: node.getStart(source),
              end: node.end,
              value: ts.isJsxText(node) || ts.isJsxAttribute(node.parent) ? `{${literal}}` : literal
            })
          }
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(source)
    for (const [index, entry] of entries.entries()) {
      if (found[index] !== entry.count)
        throw new Error(
          `Brand text owner drift: ${path}: expected ${entry.count}, got ${found[index]}`
        )
      report[entry.preserve ? 'preserved' : 'rendered'].push({
        path,
        source: entry.source,
        count: found[index],
        reason: entry.preserve
      })
    }
    edit(path, patches)
  }
  const hub = 'src/renderer/src/components/home/hub/HubHeader.tsx'
  replaceOnce(
    hub,
    "const REFER_URL = 'https://affiliate.omi.me'",
    `const REFER_URL = ${JSON.stringify(profile.links.feedback)}`
  )
  replaceOnce(
    hub,
    "const DISCORD_URL = 'https://discord.com/invite/8MP3b9ymvx'",
    `const DISCORD_URL = ${JSON.stringify(profile.links.community)}`
  )
  replaceOnce(hub, "row(Gift, 'Refer a Friend'", "row(Gift, 'Send feedback'")
  replaceOnce(
    hub,
    "{row(MessageCircle, 'Discord', () => void window.omi?.openExternalUrl?.(DISCORD_URL))}",
    "{DISCORD_URL && row(MessageCircle, 'Community', () => void window.omi?.openExternalUrl?.(DISCORD_URL))}"
  )
  replaceOnce(
    'src/main/trayState.ts',
    "    { label: 'Check for Updates', click: () => actions.checkForUpdates() },\n    { type: 'separator' },\n",
    ''
  )
  // About is a fork-owned implementation of the existing Settings tab. A local
  // disabled updater has no beta/feed/install affordance or inherited URL.
  write('fork/brand-coverage.json', JSON.stringify(report, null, 2) + '\n')
}
