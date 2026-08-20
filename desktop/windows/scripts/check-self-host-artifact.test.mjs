import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { createPackage } from '@electron/asar'
import { describe, expect, it } from 'vitest'
import { artifactHtmlErrors, packagedArtifactErrors } from './check-self-host-artifact.mjs'

describe('self-hosted Windows artifact CSP checker', () => {
  const origins = [
    'https://api.example.test',
    'https://auth.example.test',
    'wss://api.example.test'
  ]

  it('accepts a renderer constrained to signed operator origins', () => {
    const html = `<meta http-equiv="Content-Security-Policy" content="connect-src 'self' ${origins.join(' ')};">`
    expect(artifactHtmlErrors(html, origins)).toEqual([])
  })

  it.each(['api.omi.me', 'securetoken.googleapis.com', 'api.openai.com', 'cdn.jsdelivr.net'])(
    'rejects %s in the emitted artifact',
    (host) => {
      const html = `<meta http-equiv="Content-Security-Policy" content="connect-src 'self' ${origins.join(' ')} https://${host};">`
      expect(artifactHtmlErrors(html, origins)).toContainEqual(expect.stringContaining(host))
    }
  )

  it('rejects directive text that is present only inside an HTML comment', () => {
    const html = `<!-- <meta http-equiv="Content-Security-Policy" content="connect-src 'self' ${origins.join(' ')};"> -->`
    expect(artifactHtmlErrors(html, origins)).toContainEqual(
      expect.stringContaining('exactly one active CSP meta element; found 0')
    )
  })

  it('rejects a hostname that only has a signed origin as a prefix', () => {
    const html = `<meta http-equiv="Content-Security-Policy" content="connect-src 'self' https://api.example.test.evil https://auth.example.test wss://api.example.test;">`
    const errors = artifactHtmlErrors(html, origins)
    expect(errors).toContainEqual(
      expect.stringContaining('missing signed source: https://api.example.test')
    )
    expect(errors).toContainEqual(
      expect.stringContaining('unsigned source: https://api.example.test.evil')
    )
  })

  it('does not accept signed origins from a different directive', () => {
    const html = `<meta http-equiv="Content-Security-Policy" content="img-src ${origins.join(' ')}; connect-src 'self';">`
    expect(artifactHtmlErrors(html, origins)).toContainEqual(
      expect.stringContaining('connect-src is missing signed source: https://api.example.test')
    )
  })

  it('rejects an unsigned extra connect source', () => {
    const html = `<meta http-equiv="Content-Security-Policy" content="connect-src 'self' ${origins.join(' ')} https://evil.example;">`
    expect(artifactHtmlErrors(html, origins)).toContainEqual(
      expect.stringContaining('connect-src contains unsigned source: https://evil.example')
    )
  })

  it('rejects duplicate connect-src authority', () => {
    const html = `<meta http-equiv="Content-Security-Policy" content="connect-src 'self' ${origins.join(' ')}; connect-src 'self' ${origins.join(' ')};">`
    expect(artifactHtmlErrors(html, origins)).toContainEqual(
      expect.stringContaining('exactly one connect-src directive; found 2')
    )
  })
})

describe('final packaged self-host artifact checker', () => {
  const env = {
    VITE_OMI_DEPLOYMENT_PROFILE: 'self_hosted',
    VITE_OMI_API_BASE: 'https://api.operator.test',
    VITE_OMI_DESKTOP_API_BASE: 'https://desktop.operator.test',
    VITE_OMI_AUTH_BASE: 'https://auth.operator.test',
    VITE_OMI_MCP_BASE: 'https://mcp.operator.test',
    VITE_OMI_ANALYTICS_BASE: ''
  }

  async function makePackage(body) {
    const root = mkdtempSync(join(tmpdir(), 'omi-win-artifact-'))
    const source = join(root, 'source')
    const resources = join(root, 'packaged', 'resources')
    const renderer = join(source, 'out', 'renderer')
    const main = join(source, 'out', 'main')
    mkdirSync(renderer, { recursive: true })
    mkdirSync(main, { recursive: true })
    mkdirSync(resources, { recursive: true })
    const extensionNamedDirectory = join(source, 'node_modules', '@tweenjs', 'tween.js')
    mkdirSync(extensionNamedDirectory, { recursive: true })
    writeFileSync(join(extensionNamedDirectory, 'package.json'), '{"name":"@tweenjs/tween.js"}')
    writeFileSync(join(renderer, 'index.html'), body)
    writeFileSync(
      join(main, 'runtime.js'),
      `export const profile = 'self_hosted'; ${Object.values(env).join(' ')}`
    )
    writeFileSync(
      join(source, 'dependency-doc.html'),
      '<html>not a renderer and needs no application CSP</html>'
    )
    await createPackage(source, join(resources, 'app.asar'))
    return { root, packaged: join(root, 'packaged') }
  }

  it('inspects the final app.asar CSP and signed operator origins', async () => {
    const fixture = await makePackage(
      `<meta http-equiv="Content-Security-Policy" content="connect-src 'self' https://api.operator.test https://desktop.operator.test https://auth.operator.test https://mcp.operator.test wss://api.operator.test;">`
    )
    try {
      expect(packagedArtifactErrors(fixture.packaged, env)).toEqual([])
    } finally {
      rmSync(fixture.root, { recursive: true, force: true })
    }
  })

  it('rejects a populated observability credential in the final app.asar', async () => {
    const fixture = await makePackage(
      `<meta http-equiv="Content-Security-Policy" content="connect-src 'self' https://api.operator.test https://desktop.operator.test https://auth.operator.test https://mcp.operator.test wss://api.operator.test;"><script>const dsn='https://key@o1.ingest.sentry.io/2'</script>`
    )
    try {
      expect(packagedArtifactErrors(fixture.packaged, env)).toContainEqual(
        expect.stringContaining('populated Sentry DSN')
      )
    } finally {
      rmSync(fixture.root, { recursive: true, force: true })
    }
  })
})
