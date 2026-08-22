import { describe, expect, it } from 'vitest'
import {
  backendWebSocketOrigin,
  deploymentTelemetryDsn,
  rendererCorsUrlPatterns,
  resolveWindowsDeployment
} from './deploymentProfile'

const SELF_HOSTED = {
  VITE_OMI_DEPLOYMENT_PROFILE: 'self_hosted',
  VITE_OMI_IDENTITY_PROVIDER: 'better_auth',
  VITE_OMI_API_BASE: 'https://api.example.test',
  VITE_OMI_DESKTOP_API_BASE: 'https://desktop.example.test',
  VITE_OMI_AUTH_BASE: 'https://auth.example.test',
  VITE_OMI_MCP_BASE: 'https://mcp.example.test'
}

describe('Windows deployment profile', () => {
  it('resolves every self-hosted authority without a vendor fallback', () => {
    const config = resolveWindowsDeployment(SELF_HOSTED)
    expect(config).toMatchObject({
      profile: 'self_hosted',
      identityProvider: 'better_auth',
      apiBase: 'https://api.example.test',
      desktopApiBase: 'https://desktop.example.test',
      authBase: 'https://auth.example.test',
      mcpBase: 'https://mcp.example.test',
      allowDirectModelProviders: false,
      allowByok: false,
      allowGoogleConnectors: false,
      allowCloudConnectors: false
    })
    expect(config.shareBase).toBeUndefined()
    expect(config.mcpChatgptOAuthClientId).toBeUndefined()
    expect(config.mcpClaudeOAuthClientId).toBeUndefined()
    expect(config.analyticsBase).toBeUndefined()
    expect(config.updateFeed).toBeUndefined()
    expect(backendWebSocketOrigin(config)).toBe('wss://api.example.test')
    expect(rendererCorsUrlPatterns(config)).toEqual([
      'https://api.example.test/*',
      'https://desktop.example.test/*'
    ])
    expect(deploymentTelemetryDsn('https://secret@sentry.example/1', config)).toBeUndefined()
  })

  it.each([
    ['VITE_OMI_API_BASE', undefined],
    ['VITE_OMI_AUTH_BASE', undefined],
    ['VITE_OMI_MCP_BASE', 'https://api.omi.me'],
    ['VITE_OMI_DESKTOP_API_BASE', 'http://desktop.example.test']
  ])('fails closed for invalid %s', (key, value) => {
    expect(() => resolveWindowsDeployment({ ...SELF_HOSTED, [key]: value })).toThrow()
  })

  it('requires explicit paired analytics configuration in self-hosted releases', () => {
    expect(() =>
      resolveWindowsDeployment({ ...SELF_HOSTED, VITE_POSTHOG_KEY: 'operator-key' })
    ).toThrow(/analytics requires both/)
  })

  it('preserves official cloud defaults', () => {
    const config = resolveWindowsDeployment({})
    expect(config.profile).toBe('omi_cloud')
    expect(config.identityProvider).toBe('firebase')
    expect(config.apiBase).toBe('https://api.omi.me')
    expect(config.allowDirectModelProviders).toBe(true)
    expect(config.allowGoogleConnectors).toBe(true)
    expect(config.mcpChatgptOAuthClientId).toBe('omi-chatgpt-prod')
    expect(config.mcpClaudeOAuthClientId).toBe('omi-claude-prod')
    expect(rendererCorsUrlPatterns(config)).toEqual([
      'https://api.omi.me/*',
      'https://desktop-backend-hhibjajaja-uc.a.run.app/*',
      'https://us.i.posthog.com/*'
    ])
    expect(deploymentTelemetryDsn('https://public@sentry.example/1', config)).toBe(
      'https://public@sentry.example/1'
    )
  })

  it('enables only explicitly configured self-hosted public MCP OAuth clients', () => {
    const config = resolveWindowsDeployment({
      ...SELF_HOSTED,
      VITE_OMI_MCP_CHATGPT_OAUTH_CLIENT_ID: 'operator-chatgpt-public'
    })
    expect(config.allowCloudConnectors).toBe(true)
    expect(config.allowGoogleConnectors).toBe(false)
    expect(config.mcpChatgptOAuthClientId).toBe('operator-chatgpt-public')
    expect(config.mcpClaudeOAuthClientId).toBeUndefined()
  })

  it('allows an operator-owned Cloud Run origin in self-hosted releases', () => {
    const config = resolveWindowsDeployment({
      ...SELF_HOSTED,
      VITE_OMI_DESKTOP_API_BASE: 'https://operator-backend-123.a.run.app'
    })
    expect(config.desktopApiBase).toBe('https://operator-backend-123.a.run.app')
  })

  it.each([
    'https://foo.omi.me',
    'https://Foo.OMIAPI.com',
    'https://nested.foo.omi.me.',
    'https://omiapi.com'
  ])('rejects Omi-owned domain suffix %s for every signed authority', (vendorOrigin) => {
    expect(() =>
      resolveWindowsDeployment({ ...SELF_HOSTED, VITE_OMI_MCP_BASE: vendorOrigin })
    ).toThrow(/forbidden implicit vendor host/)
  })

  it.each([
    'https://DESKTOP-BACKEND-HHIBJAJAJA-UC.A.RUN.APP.',
    'https://desktop-backend-dt5lrfkkoa-uc.a.run.app'
  ])('rejects known Omi desktop Cloud Run authority %s', (managedOrigin) => {
    expect(() =>
      resolveWindowsDeployment({ ...SELF_HOSTED, VITE_OMI_DESKTOP_API_BASE: managedOrigin })
    ).toThrow(/forbidden implicit vendor host/)
  })
})
