import { describe, expect, it } from 'vitest'
import { rewriteSelfHostedCsp } from './selfHostedCsp'

const env = {
  VITE_OMI_API_BASE: 'https://api.operator.test',
  VITE_OMI_DESKTOP_API_BASE: 'https://desktop.operator.test',
  VITE_OMI_AUTH_BASE: 'https://auth.operator.test',
  VITE_OMI_MCP_BASE: 'https://mcp.operator.test'
}

describe('rewriteSelfHostedCsp', () => {
  it('replaces the real meta element without matching directive prose in a preceding comment', () => {
    const source = `<!doctype html><head>
      <!-- Removed allowances included script-src vendor; connect-src vendor; frame-src vendor. -->
      <meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src vendor; connect-src vendor; frame-src vendor;">
    </head>`

    const result = rewriteSelfHostedCsp(source, env)

    expect(result).toContain(
      '<!-- Removed allowances included script-src vendor; connect-src vendor; frame-src vendor. -->'
    )
    expect(result).toContain('<meta http-equiv="Content-Security-Policy"')
    expect(result).toContain(
      "connect-src 'self' https://api.operator.test https://desktop.operator.test https://auth.operator.test https://mcp.operator.test wss://api.operator.test"
    )
    expect(result.match(/script-src vendor/g)).toHaveLength(1)
  })

  it('fails closed when the renderer has no unique CSP authority', () => {
    expect(() =>
      rewriteSelfHostedCsp('<head><!-- connect-src only in prose --></head>', env)
    ).toThrow(/exactly one CSP meta element/)
    const duplicate =
      '<meta http-equiv="Content-Security-Policy" content="a"><meta http-equiv="Content-Security-Policy" content="b">'
    expect(() => rewriteSelfHostedCsp(duplicate, env)).toThrow(/found 2/)
  })

  it('canonicalizes every legal configured URL to the same origin contract as the artifact gate', () => {
    const canonicalized = rewriteSelfHostedCsp(
      '<meta http-equiv="Content-Security-Policy" content="default-src none">',
      {
        VITE_OMI_API_BASE: 'https://API.Operator.Test:443/',
        VITE_OMI_DESKTOP_API_BASE: 'https://DESKTOP.Operator.Test/',
        VITE_OMI_AUTH_BASE: 'https://AUTH.Operator.Test:443',
        VITE_OMI_MCP_BASE: 'https://MCP.Operator.Test/',
        VITE_OMI_ANALYTICS_BASE: 'https://ANALYTICS.Operator.Test:443/'
      }
    )

    expect(canonicalized).toContain(
      "connect-src 'self' https://api.operator.test https://desktop.operator.test https://auth.operator.test https://mcp.operator.test https://analytics.operator.test wss://api.operator.test"
    )
    expect(canonicalized).not.toMatch(/operator\.test(?::443)?\//)
  })
})
